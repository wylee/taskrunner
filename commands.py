#!/usr/bin/env python3
import os
import shutil
import sys
import unittest

if 'runcommands' not in sys.path:
    sys.path.insert(0, os.path.abspath('.'))

from runcommands import command  # noqa: E402
from runcommands.args import DISABLE, arg  # noqa: E402
from runcommands.commands import copy_file as _copy_file, local as _local  # noqa: E402
from runcommands.commands import git_version, release  # noqa: E402,F401
from runcommands.util import abort, asset_path, confirm, printer  # noqa: E402


@command
def virtualenv(where='.venv', python='python', overwrite=False):
    exists = os.path.exists(where)

    def create():
        _local((python, '-m', 'venv', where))
        printer.success(
            'Virtualenv created; activate it by running `source {where}/bin/activate`'
            .format_map(locals()))

    if exists:
        if overwrite:
            printer.warning('Overwriting virtualenv', where, 'with', python)
            shutil.rmtree(where)
            create()
        else:
            printer.info('Virtualenv', where, 'exists; pass --overwrite to re-create it')
    else:
        printer.info('Creating virtualenv', where, 'with', python)
        create()


@command
def install(where='.venv', python='python', upgrade=False, overwrite=False):
    virtualenv(where=where, python=python, overwrite=overwrite)
    pip = '{where}/bin/pip'.format(where=where)
    _local((
        pip, 'install',
        ('--upgrade', '--upgrade-strategy', 'eager') if upgrade else None,
        '--editable', '.[dev,tox]',
        ('pip', 'setuptools') if upgrade else None,
    ), echo=True)


@command
def install_completion(
        shell: arg(choices=('bash', 'fish'), help='Shell to install completion for'),
        to: arg(help='~/.bashrc.d/runcommands.rc or ~/.config/fish/runcommands.fish') = None,
        overwrite: 'Overwrite if exists' = False):
    """Install command line completion script.

    Currently, bash and fish are supported. The corresponding script
    will be copied to an appropriate directory. If the script already
    exists at that location, it will be overwritten by default.

    """
    if shell == 'bash':
        source = 'runcommands:completion/bash/runcommands.rc'
        to = to or '~/.bashrc.d'
    elif shell == 'fish':
        source = 'runcommands:completion/fish/runcommands.fish'
        to = to or '~/.config/fish/runcommands.fish'

    source = asset_path(source)
    destination = os.path.expanduser(to)

    if os.path.isdir(destination):
        destination = os.path.join(destination, os.path.basename(source))

    printer.info('Installing', shell, 'completion script to:\n    ', destination)

    if os.path.exists(destination):
        if overwrite:
            printer.info('Overwriting:\n    {destination}'.format_map(locals()))
        else:
            message = 'File exists. Overwrite?'.format_map(locals())
            overwrite = confirm(message, abort_on_unconfirmed=True)

    _copy_file(source, destination)
    printer.info('Installed; remember to:\n    source {destination}'.format_map(locals()))


@command
def test(tests=(), fail_fast=False, with_coverage=True, with_lint=True):
    original_working_directory = os.getcwd()

    if tests:
        num_tests = len(tests)
        s = '' if num_tests == 1 else 's'
        printer.header('Running {num_tests} test{s}...'.format_map(locals()))
    else:
        coverage_message = ' with coverage' if with_coverage else ''
        printer.header('Running tests{coverage_message}...'.format_map(locals()))

    runner = unittest.TextTestRunner(failfast=fail_fast)
    loader = unittest.TestLoader()

    if with_coverage:
        from coverage import Coverage
        coverage = Coverage(source=['runcommands'])
        coverage.start()

    if tests:
        for name in tests:
            runner.run(loader.loadTestsFromName(name))
    else:
        tests = loader.discover('.')
        result = runner.run(tests)
        if not result.errors:
            if with_coverage:
                coverage.stop()
                coverage.report()
            if with_lint:
                printer.header('Checking for lint...')
                # XXX: The test runner apparently changes CWD.
                os.chdir(original_working_directory)
                lint()


@command
def tox(envs: 'Pass -e option to tox with the specified environments' = (),
        recreate: 'Pass --recreate flag to tox' = False,
        clean: 'Remove tox directory first' = False):
    if clean:
        _local('rm -rf .tox', echo=True)
    _local((
        'tox',
        ('-e', ','.join(envs)) if envs else None,
        '--recreate' if recreate else None,
    ))


@command
def lint(show_errors: arg(help='Show errors') = True,
         disable_ignore: arg(inverse_option=DISABLE, help='Don\'t ignore any errors') = False,
         disable_noqa: arg(inverse_option=DISABLE, help='Ignore noqa directives') = False):
    result = _local((
        'flake8', '.',
        '--ignore=' if disable_ignore else None,
        '--disable-noqa' if disable_noqa else None,
    ), stdout='capture', raise_on_error=False)
    pieces_of_lint = len(result.stdout_lines)
    if pieces_of_lint:
        ess = '' if pieces_of_lint == 1 else 's'
        colon = ':' if show_errors else ''
        message = ['{pieces_of_lint} piece{ess} of lint found{colon}'.format_map(locals())]
        if show_errors:
            message.append(result.stdout.rstrip())
        message = '\n'.join(message)
        abort(1, message)
    else:
        printer.success('No lint found')


@command
def clean(verbose=False):
    """Clean up.

    Removes:

        - ./build/
        - ./dist/
        - **/__pycache__
        - **/*.py[co]

    Skips hidden directories.

    """
    def rm(name):
        if os.path.isfile(name):
            os.remove(name)
            if verbose:
                printer.info('Removed file:', name)
        else:
            if verbose:
                printer.info('File not present:', name)

    def rmdir(name):
        if os.path.isdir(name):
            shutil.rmtree(name)
            if verbose:
                printer.info('Removed directory:', name)
        else:
            if verbose:
                printer.info('Directory not present:', name)

    root = os.getcwd()

    rmdir('build')
    rmdir('dist')

    for path, dirs, files in os.walk(root):
        rel_path = os.path.relpath(path, root)

        if rel_path == '.':
            rel_path = ''

        if rel_path.startswith('.'):
            continue

        for d in dirs:
            if d == '__pycache__':
                rmdir(os.path.join(rel_path, d))

        for f in files:
            if f.endswith('.pyc') or f.endswith('.pyo'):
                rm(os.path.join(rel_path, f))


@command
def build_docs(source='docs', destination='docs/_build', builder='html', clean=False):
    if clean:
        printer.info('Removing {destination}...'.format_map(locals()))
        shutil.rmtree(destination)
    _local((
        'sphinx-build',
        '-b',
        builder,
        source,
        destination,
    ))


if __name__ == '__main__':
    from runcommands.__main__ import main
    sys.exit(main())
