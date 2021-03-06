#!/usr/bin/env python3
"""stempel setup file."""

# Always prefer setuptools over distutils
from setuptools import setup, find_packages
# To use a consistent encoding
from codecs import open
import os
import io
import re

here = os.path.abspath(os.path.dirname(__file__))


def read(*names, **kwargs):
    """
    Read file relative to this files location with utf8 encoding.

    Stolen from pip
    """
    with io.open(os.path.join(os.path.dirname(__file__), *names),
                 encoding=kwargs.get("encoding", "utf8")) as fp:
        return fp.read()


def find_version(*file_paths):
    """
    Find library version from __version__ atribute of package without importing.

    Stolen from pip
    """
    version_file = read(*file_paths)
    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]", version_file, re.M)
    if version_match:
        return version_match.group(1)
    raise RuntimeError("Unable to find version string.")


# Get the long description from the README file
with open(os.path.join(here, 'README.rst'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='stempel',

    # Version should comply with PEP440. For a discussion on single-sourcing
    # the version across setup.py and the project code, see
    #  https://packaging.python.org/en/latest/distributing.html
    version=find_version('stempel', '__init__.py'),

    description='Stencil TEMPlate Engineering Library',
    long_description=long_description,

    # The project's main homepage
    url='https://github.com/RRZE-HPC/stempel',

    #Author details
    author='Danilo Guerrera',
    author_email='danilo.guerrera@unibas.ch',

    #Choose your license
    license='AGPLv3',

    # See https://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 3 - Alpha',

        # Indicate who your project is intended for
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'Topic :: Scientific/Engineering',
        'Topic :: Software Development',
        'Topic :: Utilities',

        # Pick your license as you wish (should match "license" above)
        'License :: OSI Approved :: GNU Affero General Public License v3'

        # Specify the Python versions you support here. In particular, ensure
        # that you indicate wheter you support Python2, Python 3 or both.
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
    ],

    # What doesd your project relate to?
    keywords='hpc performance benchmark analysis stencil',


    # You can just specify the packages manually here if your project is
    # simple. Or you can use find_packages().
    packages=find_packages(exclude=['contrib', 'docs', 'tests*']),

    # List run-time dependencies here.  These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=[
        'kerncraft'
    ],

    python_requires='>=3.4',
    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    #extras_require={
    #    'dev': ['check-manifest'],
    #    'test': ['coverage'],
    #},

    # If there are data files included in your packages that need to be
    # installed, specify them here.  If using Python 2.6 or less, then these
    # have to be included in MANIFEST.in as well.
    package_data={
        'stempel': ['headers/*.c', 'headers/*.h', 'headers/Makefile'],
        'examples': ['machine-files/*.yaml', 'machine-files/*.yml'],
        'tests': ['testfiles/*.yml'],
    },
    include_package_data=True,

    # Although 'package_data' is the preferred approach, in some case you may
    # need to place data files outside of your packages. See:
    # http://docs.python.org/3.4/distutils/setupscript.html#installing-additional-files # noqa
    # In this case, 'data_file' will be installed into '<sys.prefix>/my_data'
    #data_files=[('my_data', ['data/data_file'])],

    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # pip to create the appropriate form of executable for the target platform.
    entry_points={
        'console_scripts': [
            'stempel=stempel.stempel:main',
            'analysis=stempel.analysis:main'
        ],
    },
)
