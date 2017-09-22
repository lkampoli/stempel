#!/usr/bin/env python

from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import division

from copy import deepcopy
import operator
import tempfile
import subprocess
import os
import os.path
import sys
import numbers
import collections
from functools import reduce
from string import ascii_letters
from itertools import chain
from collections import defaultdict
import logging
from distutils.spawn import find_executable

import sympy
from sympy.utilities.lambdify import implemented_function
from sympy.parsing.sympy_parser import parse_expr
import numpy
from six.moves import filter
from six.moves import map
from six.moves import zip_longest
import six
from pylru import lrudecorator

from pycparser import CParser, c_ast, plyparser
from pycparser.c_generator import CGenerator

import kerncraft
from kerncraft.kernel import Kernel
from kerncraft.machinemodel import MachineModel

def prefix_indent(prefix, textblock, later_prefix=' '):
    textblock = textblock.split('\n')
    s = prefix + textblock[0] + '\n'
    if len(later_prefix) == 1:
        later_prefix = ' '*len(prefix)
    s = s+'\n'.join([later_prefix+x for x in textblock[1:]])
    if s[-1] != '\n':
        return s + '\n'
    else:
        return s


def transform_multidim_to_1d_decl(decl):
    """
    Transforms ast of multidimensional declaration to a single dimension declaration.
    In-place operation!
    Returns name and dimensions of array (to be used with transform_multidim_to_1d_ref())
    """
    dims = []
    t = decl.type
    while type(t) is c_ast.ArrayDecl:
        dims.append(t.dim)
        t = t.type

    if dims:
        # Multidimensional array
        decl.type.dim = reduce(lambda l, r: c_ast.BinaryOp('*', l, r), dims)
        decl.type.type = t

    return decl.name, dims


def transform_multidim_to_1d_ref(aref, dimension_dict):
    """
    Transforms ast of multidimensional reference to a single dimension reference.
    In-place operation!
    """
    dims = []
    name = aref
    while type(name) is c_ast.ArrayRef:
        dims.append(name.subscript)
        name = name.name

    subscript_list = []
    for i, d in enumerate(dims):
        if i == 0:
            subscript_list.append(d)
        else:
            subscript_list.append(c_ast.BinaryOp('*', d, reduce(
                lambda l, r: c_ast.BinaryOp('*', l, r),
                dimension_dict[name.name][-1:-i-1:-1])))

    aref.subscript = reduce(
        lambda l, r: c_ast.BinaryOp('+', l, r), subscript_list)
    aref.name = name


def transform_array_decl_to_malloc(decl):
    """Transforms ast of "type var_name[N]" to "type* var_name = __mm_malloc(N, 32)"
    (in-place)"""
    if type(decl.type) is not c_ast.ArrayDecl:
        # Not an array declaration, can be ignored
        return

    type_ = c_ast.PtrDecl([], decl.type.type)
    decl.init = c_ast.FuncCall(
        c_ast.ID('aligned_malloc'),
        c_ast.ExprList([
            c_ast.BinaryOp(
                '*',
                c_ast.UnaryOp(
                    'sizeof',
                    c_ast.Typename(None, [], c_ast.TypeDecl(
                        None, [], decl.type.type.type))),
                decl.type.dim),
            c_ast.Constant('int', '32')]))
    decl.type = type_


def find_array_references(ast):
    """returns list of array references in AST"""
    if type(ast) is c_ast.ArrayRef:
        return [ast]
    elif type(ast) is list:
        return list(map(find_array_references, ast))
    elif ast is None:
        return []
    else:
        return reduce(operator.add,
                      [find_array_references(o[1]) for o in ast.children()], [])


# Make sure that functions will return iterable objects:
def force_iterable(f):
    def wrapper(*args, **kwargs):
        r = f(*args, **kwargs)
        if hasattr(r, '__iter__'):
            return r
        else:
            return [r]
    return wrapper


class KernelBench(Kernel):
    """
    Kernel information gathered from code using pycparser
    This version allows compilation and generation of code for benchmarking
    """
    def __init__(self, kernel_code, machine, block_factor=None, filename=None):
        super(KernelBench, self).__init__()

        self._machine = machine
        # Initialize state
        self.asm_block = None

        self.kernel_code = kernel_code
        self._filename = filename
        self.block_factor = block_factor
        # need to refer to local lextab, otherwise the systemwide lextab would
        # be imported
        parser = CParser(lextab='kerncraft.pycparser.lextab',
                         yacctab='kerncraft.pycparser.yacctab')
        try:
            self.kernel_ast = parser.parse(
            							   self._as_function(),
            							   filename=filename).ext[0].body
        except plyparser.ParseError as e:
            print('Error parsing kernel code:', e)
            sys.exit(1)

        self._process_code()

        self.check()

    def print_kernel_code(self, output_file=sys.stdout):
        print(self.kernel_code, file=output_file)

    def _as_function(self, func_name='test', filename=None):
        if filename is None:
            filename = ''
        else:
            filename ='"{}"'.format(filename)
        return '#line 0 \nvoid {}() {{\n#line 1 {}\n{}\n#line 999 \n}}'.format(func_name, filename, self.kernel_code)

    def clear_state(self):
        """Clears mutable internal states"""
        super(KernelBench, self).clear_state()
        self.asm_block = None

    def _process_code(self):
        assert type(self.kernel_ast) is c_ast.Compound, "Kernel has to be a "\
        "compound statement"
        assert all([type(s) in [c_ast.Decl, c_ast.Pragma]
                    for s in self.kernel_ast.block_items[:-1]]), \
            'all statements before the for loop need to be declarations or pragmas'
        assert type(self.kernel_ast.block_items[-1]) is c_ast.For, \
            'last statement in kernel code must be a loop'

        for item in self.kernel_ast.block_items[:-1]:
            if type(item) is c_ast.Pragma: continue
            array = type(item.type) is c_ast.ArrayDecl

            if array:
                dims = []
                t = item.type
                while type(t) is c_ast.ArrayDecl:
                    dims.append(self.conv_ast_to_sym(t.dim))
                    t = t.type

                assert len(t.type.names) == 1, "only single types are supported"
                self.set_variable(item.name, t.type.names[0], list(dims))

            else:
                assert len(item.type.type.names) == 1, \
                		"only single types are supported"
                self.set_variable(item.name, item.type.type.names[0], None)

        floop = self.kernel_ast.block_items[-1]
        self._p_for(floop)

    def conv_ast_to_sym(self, math_ast):
        """
        converts mathematical expressions containing paranthesis, addition, subtraction and
        multiplication from AST to a sympy representation.
        """
        if type(math_ast) is c_ast.ID:
            return sympy.Symbol(math_ast.name, positive=True)
        elif type(math_ast) is c_ast.Constant:
            return sympy.Integer(math_ast.value)
        else:  # elif type(dim) is c_ast.BinaryOp:
            op = {
                '*': operator.mul,
                '+': operator.add,
                '-': operator.sub
            }

            return op[math_ast.op](
                self.conv_ast_to_sym(math_ast.left),
                self.conv_ast_to_sym(math_ast.right))

    def _get_offsets(self, aref, dim=0):
        """
        Returns a list of offsets of an ArrayRef object in all dimensions
        the index order is right to left (c-code order).
        e.g. c[i+1][j-2] -> [-2, +1]
        if aref is actually an ID, None will be returned
        """
        if isinstance(aref, c_ast.ID):
            return None

        # Check for restrictions
        assert type(aref.name) in [c_ast.ArrayRef, c_ast.ID], \
            "array references must only be used with variables or other array references"
        assert type(aref.subscript) in [c_ast.ID, c_ast.Constant, c_ast.BinaryOp], \
            'array subscript must only contain variables or binary operations'

        idxs = []

        # Convert subscript to sympy and append
        idxs.append(self.conv_ast_to_sym(aref.subscript))

        # Check for more indices (multi-dimensional access)
        if type(aref.name) is c_ast.ArrayRef:
            idxs += self._get_offsets(aref.name, dim=dim+1)

        # Reverse to preserver order (the subscripts in the AST are traversed backwards)
        if dim == 0:
            idxs.reverse()

        return idxs

    @classmethod
    def _get_basename(cls, aref):
        """
        returns base name of ArrayRef object
        e.g. c[i+1][j-2] -> 'c'
        """

        if isinstance(aref.name, c_ast.ArrayRef):
            return cls._get_basename(aref.name)
        elif isinstance(aref.name, six.string_types):
            return aref.name
        else:
            return aref.name.name

    def _p_for(self, floop):
        # Check for restrictions
        assert type(floop) is c_ast.For, "May only be a for loop"
        assert hasattr(floop, 'init') and hasattr(floop, 'cond') and hasattr(floop, 'next'), \
            "Loop must have initial, condition and next statements."
        assert type(floop.init) is c_ast.DeclList, \
            "Initialization of loops need to be declarations."
        assert len(floop.init.decls) == 1, "Only single declaration is allowed in init. of loop."
        assert floop.cond.op in '<', "only lt (<) is allowed as loop condition"
        assert type(floop.cond.left) is c_ast.ID, 'left of cond. operand has to be a variable'
        assert type(floop.cond.right) in [c_ast.Constant, c_ast.ID, c_ast.BinaryOp], \
            'right of cond. operand has to be a constant, a variable or a binary operation'
        assert type(floop.next) in [c_ast.UnaryOp, c_ast.Assignment], \
            'next statement has to be a unary or assignment operation'
        assert floop.next.op in ['++', 'p++', '+='], 'only ++ and += next operations are allowed'
        assert type(floop.stmt) in [c_ast.Compound, c_ast.Assignment, c_ast.For], \
            'the inner loop may contain only assignments or compounds of assignments'

        if type(floop.cond.right) is c_ast.ID:
            const_name = floop.cond.right.name
            iter_max = sympy.Symbol(const_name, positive=True)
        elif type(floop.cond.right) is c_ast.Constant:
            iter_max = sympy.Integer(floop.cond.right.value)
        else:  # type(floop.cond.right) is c_ast.BinaryOp
            bop = floop.cond.right
            assert bop.op in '+-*', ('only addition (+), substraction (-) and multiplications (*) '
                                     'are accepted operators')
            iter_max = self.conv_ast_to_sym(bop)

        iter_min = self.conv_ast_to_sym(floop.init.decls[0].init)

        if type(floop.next) is c_ast.Assignment:
            assert type(floop.next.lvalue) is c_ast.ID, \
                'next operation may only act on loop counter'
            assert type(floop.next.rvalue) is c_ast.Constant, 'only constant increments are allowed'
            assert floop.next.lvalue.name == floop.cond.left.name == floop.init.decls[0].name, \
                'initial, condition and next statement of for loop must act on same loop ' \
                'counter variable'
            step_size = int(floop.next.rvalue.value)
        else:
            assert type(floop.next.expr) is c_ast.ID, 'next operation may only act on loop counter'
            assert floop.next.expr.name == floop.cond.left.name == floop.init.decls[0].name, \
                'initial, condition and next statement of for loop must act on same loop ' \
                'counter variable'
            assert isinstance(floop.next, c_ast.UnaryOp), 'only assignment or unary operations ' \
                'are allowed for next statement of loop.'
            assert floop.next.op in ['++', 'p++', '--', 'p--'], 'Unary operation can only be ++ ' \
                'or -- in next statement'
            if floop.next.op in ['++', 'p++']:
                step_size = sympy.Integer('1')
            else:  # floop.next.op in ['--', 'p--']:
                step_size = sympy.Integer('-1')

        # Document for loop stack
        self._loop_stack.append(
            # (index name, min, max, step size)
            (floop.init.decls[0].name, iter_min, iter_max, step_size)
        )

        # Traverse tree
        if type(floop.stmt) is c_ast.For:
            self._p_for(floop.stmt)
        elif type(floop.stmt) is c_ast.Assignment:
            self._p_assignment(floop.stmt)
        # Handle For if it is the last statement, only preceeded by Pragmas
        elif type(floop.stmt.block_items[-1]) is c_ast.For and \
                all([type(s) == c_ast.Pragma for s in floop.stmt.block_items[:-1]]):
            self._p_for(floop.stmt.block_items[-1])
        else:  # type(floop.stmt) is c_ast.Compound
            # Handle Assignments
            for assgn in floop.stmt.block_items:
                self._p_assignment(assgn)

    def _p_assignment(self, stmt):
        # Check for restrictions
        assert type(stmt) is c_ast.Assignment, \
            "Only assignment statements are allowed in loops."
        assert type(stmt.lvalue) in [c_ast.ArrayRef, c_ast.ID], \
            "Only assignment to array element or varialbe is allowed."

        write_and_read = False
        if stmt.op != '=':
            write_and_read = True
            op = stmt.op.strip('=')
            self._flops[op] = self._flops.get(op, 0)+1

        # Document data destination
        # self._destinations[dest name] = [dest offset, ...])
        self._destinations.setdefault(self._get_basename(stmt.lvalue), [])
        self._destinations[self._get_basename(stmt.lvalue)].append(
            self._get_offsets(stmt.lvalue))

        if write_and_read:
            # this means that +=, -= or something of that sort was used
            self._sources.setdefault(self._get_basename(stmt.lvalue), [])
            self._sources[self._get_basename(stmt.lvalue)].append(
                self._get_offsets(stmt.lvalue))

        # Traverse tree
        self._p_sources(stmt.rvalue)

    def _p_sources(self, stmt):
        sources = []
        assert type(stmt) in \
            [c_ast.ArrayRef, c_ast.Constant, c_ast.ID, c_ast.BinaryOp, c_ast.UnaryOp], \
            'only references to arrays, constants and variables as well as binary operations ' + \
            'are supported'
        assert type(stmt) is not c_ast.UnaryOp or stmt.op in ['-', '--', '++', 'p++', 'p--'], \
            'unary operations are only allowed with -, -- and ++'

        if type(stmt) in [c_ast.ArrayRef, c_ast.ID]:
            # Document data source
            bname = self._get_basename(stmt)
            self._sources.setdefault(bname, [])
            self._sources[bname].append(self._get_offsets(stmt))
        elif type(stmt) is c_ast.BinaryOp:
            # Traverse tree
            self._p_sources(stmt.left)
            self._p_sources(stmt.right)

            self._flops[stmt.op] = self._flops.get(stmt.op, 0)+1
        elif type(stmt) is c_ast.UnaryOp:
            self._p_sources(stmt.expr)
            self._flops[stmt.op] = self._flops.get(stmt.op[-1], 0)+1

        return sources

    def as_code(self, type_='likwid'):
        """
        generates compilable source code from AST
        *type* can be iaca or likwid.
        """
        assert self.kernel_ast is not None, "AST does not exist, this could be due to running of " \
                                            "kernel description rather than code."

        ast = deepcopy(self.kernel_ast)
        declarations = [d for d in ast.block_items if type(d) is c_ast.Decl]

        # transform multi-dimensional declarations to one dimensional references
        array_dimensions = dict(list(map(transform_multidim_to_1d_decl, declarations)))
        # transform to pointer and malloc notation (stack can be too small)
        list(map(transform_array_decl_to_malloc, declarations))

        # add declarations for constants from the executable command line
        # i = 1  # subscript for cli input
        # for k in self.constants:
        #     # cont int N = atoi(argv[1])
        #     type_decl = c_ast.TypeDecl(k.name, ['const'], c_ast.IdentifierType(['int']))
        #     init = c_ast.FuncCall(
        #         c_ast.ID('atoi'),
        #         c_ast.ExprList([c_ast.ArrayRef(c_ast.ID('argv'), c_ast.Constant('int', str(i)))]))
        #     i += 1
        #     decl = c_ast.Decl(k.name, ['const'], [], [], type_decl, init, None)
        #     ast.block_items.insert(0, decl)

        # add declarations for constants from value passed to stempel
        # for name, value in list(self.constants.items()):
        #     type_decl = c_ast.TypeDecl(str(name), [], c_ast.IdentifierType(['int']))
        #     decl = c_ast.Decl(
        #         name, ['const'], [], [],
        #             type_decl, c_ast.Constant('int', str(value)), None)
        #     ast.block_items.insert(0, decl)

        if type_ == 'likwid':
            # Call likwid_markerInit()
            ast.block_items.insert(0, c_ast.FuncCall(c_ast.ID('likwid_markerInit'), None))
            # Call likwid_markerThreadInit()
            ast.block_items.insert(1, c_ast.FuncCall(c_ast.ID('likwid_markerThreadInit'), None))
            # Call likwid_markerClose()
            ast.block_items.append(c_ast.FuncCall(c_ast.ID('likwid_markerClose'), None))

        # inject array initialization
        for d in declarations:
            i = ast.block_items.index(d)

            # Build ast to inject
            if array_dimensions[d.name]:
                # this is an array, we need a for loop to initialize it
                # for(init; cond; next) stmt

                # Init: int i = 0;
                counter_name = 'i'
                while counter_name in array_dimensions:
                    counter_name = chr(ord(counter_name)+1)

                init = c_ast.DeclList([
                    c_ast.Decl(
                        counter_name, [], [], [], c_ast.TypeDecl(
                            counter_name, [], c_ast.IdentifierType(['int'])),
                        c_ast.Constant('int', '0'),
                        None)],
                    None)

                # Cond: i < ... (... is length of array)
                grid_size = reduce(lambda l, r: c_ast.BinaryOp('*', l, r), array_dimensions[d.name])
                cond = c_ast.BinaryOp('<', c_ast.ID(counter_name), grid_size)


                # Next: i++
                next_ = c_ast.UnaryOp('++', c_ast.ID(counter_name))

                # Statement
                stmt = c_ast.Assignment(
                    '=',
                    c_ast.ArrayRef(c_ast.ID(d.name), c_ast.ID(counter_name)),
                    c_ast.Constant('float', '0.23'))

                ast.block_items.insert(i+1, c_ast.For(init, cond, next_, stmt))

                # inject dummy access to arrays, so compiler does not over-optimize code
                # with if around it, so code will actually run
                ast.block_items.insert(
                    i+2, c_ast.If(
                        cond=c_ast.ID('var_false'),
                        iftrue=c_ast.Compound([
                            c_ast.FuncCall(
                                c_ast.ID('dummy'),
                                c_ast.ExprList([c_ast.ID(d.name)]))]),
                        iffalse=None))
            else:
                # this is a scalar, so a simple Assignment is enough
                ast.block_items.insert(
                    i+1, c_ast.Assignment('=', c_ast.ID(d.name), c_ast.Constant('float', '0.23')))

                # inject dummy access to scalar, so compiler does not over-optimize code
                # TODO put if around it, so code will actually run
                ast.block_items.insert(
                    i+2, c_ast.If(
                        cond=c_ast.ID('var_false'),
                        iftrue=c_ast.Compound([
                            c_ast.FuncCall(
                                c_ast.ID('dummy'),
                                c_ast.ExprList([c_ast.UnaryOp('&', c_ast.ID(d.name))]))]),
                        iffalse=None))

        # transform multi-dimensional array references to one dimensional references
        list(map(lambda aref: transform_multidim_to_1d_ref(aref, array_dimensions),
                 find_array_references(ast)))

        dummies = []
        # Make sure nothing gets removed by inserting dummy calls
        for d in declarations:
            if array_dimensions[d.name]:
                dummies.append(c_ast.If(
                    cond=c_ast.ID('var_false'),
                    iftrue=c_ast.Compound([
                        c_ast.FuncCall(
                            c_ast.ID('dummy'),
                            c_ast.ExprList([c_ast.ID(d.name)]))]),
                    iffalse=None))
            else:
                dummies.append(c_ast.If(
                    cond=c_ast.ID('var_false'),
                    iftrue=c_ast.Compound([
                        c_ast.FuncCall(
                            c_ast.ID('dummy'),
                            c_ast.ExprList([c_ast.UnaryOp('&', c_ast.ID(d.name))]))]),
                    iffalse=None))

        if type_ == 'likwid':
            # Instrument the outer for-loop with likwid
            ast.block_items.insert(-2, c_ast.FuncCall(
                c_ast.ID('likwid_markerStartRegion'),
                c_ast.ExprList([c_ast.Constant('string', '"Sweep"')])))

            #add declaration of the block
            if self.block_factor:
                type_decl = c_ast.TypeDecl('block_factor', [], c_ast.IdentifierType(['int']))
                decl = c_ast.Decl(
                    'block_factor', ['const'], [], [],
                    type_decl, c_ast.Constant('int', str(self.block_factor)), None)
                ast.block_items.insert(-3, decl)

                #add it to the list of declarations, so it gets passed to the kernel_loop
                declarations.append(decl)

            # Wrap everything in a loop
            # int repeat = atoi(argv[2])
            type_decl = c_ast.TypeDecl('repeat', [], c_ast.IdentifierType(['int']))
            # init = c_ast.FuncCall(
            #     c_ast.ID('atoi'),
            #     c_ast.ExprList([c_ast.ArrayRef(
            #         c_ast.ID('argv'), c_ast.Constant('int', str(len(self.constants)+2)))]))
            # ast.block_items.insert(-3, c_ast.Decl(
            #     'repeat', ['const'], [], [],
            #     type_decl, init, None))
            ast.block_items.insert(-3, c_ast.Decl(
                'repeat', ['const'], [], [],
                type_decl, c_ast.Constant('int', '1'), None))

            #timing variables declaration and initialisation
            type_decl = c_ast.TypeDecl('runtime', [], c_ast.IdentifierType(['double']))
            ast.block_items.insert(-3, c_ast.Decl(
                'runtime', ['const'], [], [],
                type_decl, c_ast.Constant('double', '0.0'), None))

            
            decl = c_ast.Decl('wct_start', [], [], [], c_ast.TypeDecl(
                'wct_start', [], c_ast.IdentifierType(['double'])
            ), None, None)
            ast.block_items.insert(-3, decl)
            decl = c_ast.Decl('wct_end', [], [], [], c_ast.TypeDecl(
                'wct_end', [], c_ast.IdentifierType(['double'])
            ), None, None)
            ast.block_items.insert(-3, decl)
            decl = c_ast.Decl('cput_start', [], [], [], c_ast.TypeDecl(
                'cput_start', [], c_ast.IdentifierType(['double'])
            ), None, None)
            ast.block_items.insert(-3, decl)
            decl = c_ast.Decl('cput_end', [], [], [], c_ast.TypeDecl(
                'cput_end', [], c_ast.IdentifierType(['double'])
            ), None, None)
            ast.block_items.insert(-3, decl)


            #call the timing function at the beginning
            start_timing = c_ast.FuncCall(c_ast.ID('timing'),
                    c_ast.ExprList([c_ast.UnaryOp('&', c_ast.ID('wct_start')),
                        c_ast.UnaryOp('&', c_ast.ID('cput_start'))]))

            # take out the for loop that will be written in a function on top
            forloop = ast.block_items.pop(-2)

            #creating a list of pointer to all the variables of type pointer
            pointers_list = [c_ast.Typename(None, [], c_ast.PtrDecl(
                [], c_ast.TypeDecl(d.name, [], d.type.type))) for d in declarations if type(d.type) is c_ast.PtrDecl]
            first_array_name = pointers_list[0].type.type.declname
            #get the number of dimensions by fetching the size of the first array
            mydims = len(array_dimensions.get(first_array_name))
            #get the type of the first array
            first_array_type = pointers_list[0].type.type.type.type.names[0]

            # for(n = 0; n < repeat; n++) {...}
            index_name = 'n'
            init = c_ast.DeclList([
                    c_ast.Decl(
                        index_name, [], [], [], c_ast.TypeDecl(
                            index_name, [], c_ast.IdentifierType(['int'])),
                        c_ast.Constant('int', '0'),
                        None)],
                    None)
            cond = c_ast.BinaryOp( '<', c_ast.ID(index_name), c_ast.ID('repeat'))
            next_ = c_ast.UnaryOp('++', c_ast.ID(index_name))
            #stmt = c_ast.Compound([ast.block_items.pop(-2)]+dummies)
            stmt = c_ast.FuncCall(c_ast.ID('kernel_loop'),
            		c_ast.ExprList([c_ast.ID(d.name) for d in declarations]))
            swap_tmp = c_ast.Assignment('=', c_ast.ID('tmp'),
                c_ast.ID(pointers_list[0].type.type.declname))
            swap_grid = c_ast.Assignment('=', c_ast.ID(pointers_list[0].type.type.declname),
                c_ast.ID(pointers_list[1].type.type.declname))
            last_swap = c_ast.Assignment('=', c_ast.ID(pointers_list[1].type.type.declname),
                c_ast.ID(pointers_list[0].type.type.declname))
            stmt = c_ast.Compound([stmt, swap_tmp, swap_grid, last_swap])
            myfor = c_ast.For(init, cond, next_, stmt)
            
            #call the timing function at the beginning
            end_timing = c_ast.FuncCall(c_ast.ID('timing'),
                    c_ast.ExprList([c_ast.UnaryOp('&', c_ast.ID('wct_end')),
                        c_ast.UnaryOp('&', c_ast.ID('cput_end'))]))

            update_runtime = c_ast.Assignment('=', c_ast.ID('runtime'),
                c_ast.BinaryOp( '-', c_ast.ID('wct_end'), c_ast.ID('wct_start')))

            update_iter = c_ast.Assignment('*=', c_ast.ID('repeat'),
                c_ast.Constant('int', '2'))

            #while(runtime<.5) {...}
            cond = c_ast.BinaryOp( '<', c_ast.ID('runtime'), c_ast.Constant('double', '0.5'))
            stmt = c_ast.Compound([start_timing, myfor, end_timing, update_runtime, update_iter])

            ast.block_items.insert(-1, c_ast.While(cond, stmt))

            #close the region "Sweep" of likwid
            ast.block_items.insert(-1, c_ast.FuncCall(
                c_ast.ID('likwid_markerStopRegion'),
                c_ast.ExprList([c_ast.Constant('string', '"Sweep"')])))

            #the variable repeat must be divided by 2 since in the last loop was doubled before exiting
            ast.block_items.insert(-1, c_ast.Assignment('/=', c_ast.ID('repeat'), c_ast.Constant('int', '2')))

            #calculate the size of the grid, taking the letters representing its dimensions from the array of constants
            size = '(' + ' * '.join(k.name for k in self.constants) + ')'

            decl = c_ast.Decl('tmp', [], [], [], c_ast.PtrDecl(
                [], c_ast.TypeDecl('tmp', [],
                    pointers_list[0].type.type.type.type)),
                None, None)
            ast.block_items.insert(-5, decl)

            #creating a list of standard types for all the non-pointer variables
            variables_list = [c_ast.Typename(None, [], c_ast.TypeDecl(d.name, [], d.type.type)) for d in declarations if type(d.type) is c_ast.TypeDecl]


            #generate the LUP expression according to the number of dimensions
            # it is necessary to do so since we do not know a priori how many nested for we have
            if mydims == 1:
                LUP_expression = forloop.cond.right#c_ast.ExprList([forloop.cond.right])
            elif mydims == 2:
                LUP_expression = c_ast.BinaryOp('*', forloop.cond.right, forloop.stmt.cond.right)
            elif mydims == 3:
                LUP_expression = c_ast.BinaryOp('*', c_ast.BinaryOp('*', forloop.cond.right, forloop.stmt.cond.right), forloop.stmt.stmt.cond.right)
            
            #we build MLUP. should be like: (double)iter*(size_x-ghost)*(size_y-ghost)*(size_z-ghost)/runtime/1000000.
            LUP_expression = c_ast.BinaryOp('*', c_ast.ID('repeat'), LUP_expression)
            #cast it to double since the first variables are ints
            LUP_expr_cast =  c_ast.Cast(c_ast.IdentifierType(['double']), LUP_expression)
            #we put all together to get MLUP
            MLUP = c_ast.BinaryOp('/', LUP_expr_cast, c_ast.BinaryOp('*', c_ast.ID('runtime'), c_ast.Constant('double', '1000000.')))
            
            #insert the printf of the stats
            ast.block_items.insert(-1, c_ast.FuncCall( c_ast.ID('printf'),
                c_ast.ExprList([c_ast.Constant('string', '"size: %d    time: %lf    iter: %d    MLUP/s: %lf"'),
                    c_ast.ID(size), c_ast.ID('runtime'), c_ast.ID('repeat'), MLUP])))

        else:
            ast.block_items += dummies


        # embed compound into main FuncDecl
        decl = c_ast.Decl('main', [], [], [], c_ast.FuncDecl(c_ast.ParamList([
            c_ast.Typename(None, [], c_ast.TypeDecl('argc', [], c_ast.IdentifierType(['int']))),
            c_ast.Typename(None, [], c_ast.PtrDecl([], c_ast.PtrDecl(
                [], c_ast.TypeDecl('argv', [], c_ast.IdentifierType(['char'])))))]),
            c_ast.TypeDecl('main', [], c_ast.IdentifierType(['int']))),
            None, None)

        ast = c_ast.FuncDef(decl, None, ast)

        # embed Compound AST into FileAST
        #ast = c_ast.FileAST([ast])

        myvariables = []
        for i in range(0, mydims):
                myvariables.append(chr(ord('i')+i))

        pragma_int = c_ast.Pragma('omp for private({}) schedule(runtime)'.format(','.join(myvariables)))

        #declaring the function of the kernel with the parameters list built before
        decl = c_ast.Decl('kernel_loop', [], [], [], c_ast.FuncDecl(
            c_ast.ParamList(pointers_list + variables_list),
            c_ast.TypeDecl('kernel_loop', [], c_ast.IdentifierType(['void']))),
            None, None)

        if self.block_factor:
            if mydims == 2: #blocking on the inner-most loop
                beginning = myvariables[0]+'b'
                end = myvariables[0]+'end'
                pragma = c_ast.Pragma('omp parallel for private({}, {})'.format(beginning, end))

                init = c_ast.DeclList([
                        c_ast.Decl(
                            beginning, [], [], [], c_ast.TypeDecl(
                                beginning, [], c_ast.IdentifierType(['int'])),
                            forloop.stmt.init.decls[0].init,
                            None)],
                        None)
                # for(jb = 1; jb < N-1; jb+=block_factor) {...}reduce(lambda l, r: c_ast.BinaryOp('*', l, r), array_dimensions[d.name]))
                cond = c_ast.BinaryOp( '<', c_ast.ID(beginning), forloop.stmt.cond.right)
                next_ = c_ast.BinaryOp('+=', c_ast.ID(beginning), c_ast.ID('block_factor'))
                #stmt = c_ast.Compound([ast.block_items.pop(-2)]+dummies)
                
                assign = c_ast.Assignment('=', c_ast.ID(end), c_ast.FuncCall(c_ast.ID('min'),
                        c_ast.ExprList([c_ast.BinaryOp('+', c_ast.ID(beginning), c_ast.ID('block_factor')), forloop.stmt.cond.right])))
                
                forloop.stmt.init.decls[0].init = c_ast.ID(beginning)
                forloop.stmt.cond.right = c_ast.ID(end)

                mycompound = c_ast.Compound([assign, pragma_int, forloop]+dummies)
                
                newfor = c_ast.For(init, cond, next_, mycompound)

                mycompound = c_ast.Compound([pragma, newfor])
                                    
            elif mydims == 3: #blocking on the middle loop
                beginning = myvariables[1]+'b'
                end = myvariables[1]+'end'
                pragma = c_ast.Pragma('omp parallel for private({}, {})'.format(beginning, end))

                init = c_ast.DeclList([
                        c_ast.Decl(
                            beginning, [], [], [], c_ast.TypeDecl(
                                beginning, [], c_ast.IdentifierType(['int'])),
                            forloop.stmt.init.decls[0].init,
                            None)],
                        None)
                # for(jb = 1; jb < N-1; jb+=block_factor) {...}reduce(lambda l, r: c_ast.BinaryOp('*', l, r), array_dimensions[d.name]))
                cond = c_ast.BinaryOp( '<', c_ast.ID(beginning), forloop.stmt.cond.right)
                next_ = c_ast.BinaryOp('+=', c_ast.ID(beginning), c_ast.ID('block_factor'))
                #stmt = c_ast.Compound([ast.block_items.pop(-2)]+dummies)
                
                assign = c_ast.Assignment('=', c_ast.ID(end), c_ast.FuncCall(c_ast.ID('min'),
                        c_ast.ExprList([c_ast.BinaryOp('+', c_ast.ID(beginning), c_ast.ID('block_factor')), forloop.stmt.cond.right])))
                mycompound = c_ast.Compound([assign, pragma_int, forloop]+dummies)
                
                newfor = c_ast.For(init, cond, next_, mycompound)

                mycompound = c_ast.Compound([pragma, newfor])

        else:
            mycompound = c_ast.Compound([pragma_int, forloop]+dummies)

        #mycode = CGenerator().visit(mycompound)

        #logging.warning(type(forloop))
        ast1 = c_ast.FuncDef(decl, None, mycompound)
        ast = c_ast.FileAST([ast1, ast])
        #ast.ext.insert(0, decl)

        #ast.ext.insert(0, forloop)
        # add dummy function declaration
        decl = c_ast.Decl('dummy', [], [], [], c_ast.FuncDecl(
            c_ast.ParamList([c_ast.Typename(None, [], c_ast.PtrDecl(
                [], c_ast.TypeDecl(None, [], c_ast.IdentifierType(['double']))))]),
            c_ast.TypeDecl('dummy', [], c_ast.IdentifierType(['void']))),
            None, None)
        ast.ext.insert(0, decl)

        # add external var_false declaration
        decl = c_ast.Decl('var_false', [], ['extern'], [], c_ast.TypeDecl(
                'var_false', [], c_ast.IdentifierType(['int'])
            ), None, None)
        ast.ext.insert(1, decl)


        # convert to code string
        code = CGenerator().visit(ast)

        #add empty line on top
        code = '\n' + code
        #add defines of the variables storing the size of the dimensions
        for name, value in list(self.constants.items()):
            line = '#define {} {}L\n'.format(name, value)
            code = line + code

        # add "#include"s for dummy, var_false and stdlib (for malloc)
        code = '#include <stdlib.h>\n\n' + code
        code = '#include "kerncraft.h"\n' + code
        code = '#include "timing.h"\n' + code
        if type_ == 'likwid':
            code = '#include <likwid.h>\n' + code

        # remove trailing ";": it must be fixed in the place where it is
        # accidentally added, i.e. when adding the function kernel_loop.
        # just a workaround
        code = code.rstrip()
       	if code.endswith(';'):
       		code = code = code[0:-1]
       	#return mycode
        return code


    def assemble(self, in_filename, out_filename=None, iaca_markers=True,
                 asm_block='auto', asm_increment=0, verbose=False):
        """
        Assembles *in_filename* to *out_filename*.
        If *out_filename* is not given a new file will created either temporarily or according
        to kernel file location.
        if *iaca_marked* is set to true, markers are inserted around the block with most packed
        instructions or (if no packed instr. were found) the largest block and modified file is
        saved to *in_file*.
        *asm_block* controls how the to-be-marked block is chosen. "auto" (default) results in
        the largest block, "manual" results in interactive and a number in the according block.
        *asm_increment* is the increment of the store pointer during each iteration of the ASM block
        if it is 0 (default), automatic detection will be use and might lead to an interactive user
        interface.
        Returns two-tuple (filepointer, filename) to temp binary file.
        """
        if not out_filename:
            if self._filename:
                out_filename = os.path.abspath(os.path.splitext(self._filename)[0])
            else:
                out_filename = tempfile.mkstemp()

        ccompiler, compiler_args = self._machine.get_compiler()

        cmd = [compiler, os.path.basename(in_filename), 'dummy.s', '-o', out_filename]
        if verbose:
            print('Executing (assemble): ', ' '.join(cmd))

        try:
            # Assemble all to a binary
            subprocess.check_output(
                cmd,
                cwd=os.path.dirname(os.path.realpath(in_filename)))
        except subprocess.CalledProcessError as e:
            print("Assembly failed:", e, file=sys.stderr)
            sys.exit(1)

        return out_filename

    def compile(self, verbose=False):
        """
        Compiles source (from as_code(type_)) to assembly.
        Returns two-tuple (filepointer, filename) to assembly file.
        Output can be used with Kernel.assemble()
        """
        compiler, compiler_args = self._machine.get_compiler()

        if not self._filename:
            in_file = tempfile.NamedTemporaryFile(
                suffix='_compilable.c', mode='w', encoding='ascii'
            )
        else:
            in_file = open(self._filename+"_compilable.c", 'w')

        in_file.write(self.as_code())
        in_file.flush()

        compiler_args += ['-std=c99']

        cmd = ([compiler] +
               compiler_args +
               [os.path.basename(in_file.name),
                '-S',
                '-I'+os.path.abspath(os.path.dirname(os.path.realpath(__file__)))+'/headers/'])

        if verbose:
            print('Executing (compile): ', ' '.join(cmd))

        try:
            subprocess.check_output(
                cmd,
                cwd=os.path.dirname(os.path.realpath(in_file.name)))

            subprocess.check_output(
                [compiler] + compiler_args + [
                    os.path.abspath(os.path.dirname(os.path.realpath(__file__))+'/headers/dummy.c'),
                    '-S'],
                cwd=os.path.dirname(os.path.realpath(in_file.name)))
        except subprocess.CalledProcessError as e:
            print("Compilation failed:", e, file=sys.stderr)
            sys.exit(1)
        finally:
            in_file.close()

        # Let's return the out_file name
        return os.path.splitext(in_file.name)[0]+'.s'

    def build(self, compiler=None, compiler_args=None, lflags=None, verbose=False):
        """
        compiles source to executable with likwid capabilities
        returns the executable name
        """

        #compiler, compiler_args = self._machine.get_compiler()
        if not compiler:
            compiler = 'gcc'
        if not compiler_args:
            compiler_args = ['-O3']

        if not (('LIKWID_INCLUDE' in os.environ or 'LIKWID_INC' in os.environ) and
                'LIKWID_LIB' in os.environ):
            print('Could not find LIKWID_INCLUDE and LIKWID_LIB environment variables',
                  file=sys.stderr)
            sys.exit(1)

        compiler_args += [
            '-std=c99',
            '-I'+os.path.abspath(os.path.dirname(os.path.realpath(__file__)))+'/headers/',
            os.environ.get('LIKWID_INCLUDE', ''),
            os.environ.get('LIKWID_INC', ''), '-llikwid']

        # This is a special case for unittesting
        if os.environ.get('LIKWID_LIB') == '':
            compiler_args = compiler_args[:-1]

        if lflags is None:
            lflags = []
        lflags += os.environ['LIKWID_LIB'].split(' ') + ['-pthread']
        compiler_args += os.environ['LIKWID_LIB'].split(' ') + ['-pthread']


        if not self._filename:
            source_file = tempfile.NamedTemporaryFile(
                suffix='_compilable.c', mode='w', encoding='ascii'
            )
        else:
            source_file = open(self._filename+"_compilable.c", 'w')

        source_file.write(self.as_code(type_='likwid'))
        source_file.flush()

        infiles = [os.path.abspath(os.path.dirname(os.path.realpath(__file__)))+'/headers/dummy.c',
                   source_file.name]
        if self._filename:
            outfile = os.path.abspath(os.path.splitext(self._filename)[0]+'.likwid_marked')
        else:
            outfile = tempfile.mkstemp(suffix='.likwid_marked')
        cmd = [compiler] + infiles + compiler_args + ['-o', outfile]
        # remove empty arguments
        cmd = list(filter(bool, cmd))
        if verbose:
            print('Executing (build): ', ' '.join(cmd))
        try:
            subprocess.check_output(cmd)
        except subprocess.CalledProcessError as e:
            print("Build failed:", e, file=sys.stderr)
            sys.exit(1)
        finally:
            source_file.close()

        return outfile


    def perfctr(self, cmd, group='MEM', cpu='S0:0', code_markers=True, pin=True):
        '''
        runs *cmd* with likwid-perfctr and returns result as dict
        *group* may be a performance group known to likwid-perfctr or an event string.
        Only works with single core!
        '''

        # Making sure iaca.sh is available:
        if find_executable('likwid-perfctr') is None:
            print("likwid-perfctr was not found. Make sure likwid is installed and found in PATH.",
                  file=sys.stderr)
            sys.exit(1)

        # FIXME currently only single core measurements support!
        perf_cmd = ['likwid-perfctr', '-f', '-O', '-g', group]

        if pin:
            perf_cmd += ['-C', cpu]
        else:
            perf_cmd += ['-c', cpu]

        if code_markers:
            perf_cmd.append('-m')

        perf_cmd += cmd
        if self._args.verbose > 1:
            print(' '.join(perf_cmd))
        try:
            output = subprocess.check_output(perf_cmd).decode('utf-8').split('\n')
        except subprocess.CalledProcessError as e:
            print("Executing benchmark failed: {!s}".format(e), file=sys.stderr)
            sys.exit(1)

        results = {}
        ignore = True
        for l in output:
            l = l.split(',')
            try:
                # Metrics
                results[l[0]] = float(l[1])
            except:
                pass
            try:
                # Event counters
                counter_value = int(l[2])
                if re.fullmatch(r'[A-Z0-9_]+', l[0]) and re.fullmatch(r'[A-Z0-9]+', l[1]):
                    results.setdefault(l[0], {})
                    results[l[0]][l[1]] = counter_value
            except (IndexError, ValueError):
                pass

        return results