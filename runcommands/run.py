import os
import sys
from configparser import ConfigParser

from . import __version__
from .command import command, Command
from .const import DEFAULT_COMMANDS_MODULE, DEFAULT_CONFIG_FILE
from .exc import RunCommandsError, RunnerError
from .runner import CommandRunner
from .util import printer


def run(config,
        module=DEFAULT_COMMANDS_MODULE,
        # config
        config_file=None,
        env=None,
        # options
        options={},
        version=None,
        # output
        echo=False,
        hide=False,
        debug=False,
        # info/help
        info=False,
        list_commands=False,
        list_envs=False):
    """Run one or more commands in succession.

    For example, assume the commands ``local`` and ``remote`` have been
    defined; the following will run ``ls`` first on the local host and
    then on the remote host::

        runcommands local ls remote <host> ls

    When a command name is encountered in ``argv``, it will be considered
    the starting point of the next command *unless* the previous item in
    ``argv`` was an option like ``--xyz`` that expects a value (i.e.,
    it's not a flag).

    To avoid ambiguity when an option value matches a command name, the
    value can be prepended with a colon to force it to be considered
    a value and not a command name.

    """
    argv = config.argv
    run_argv = config.run_argv
    command_argv = config.command_argv
    run_args = config.run_args

    show_info = info or list_commands or list_envs or not command_argv or debug
    print_and_exit = info or list_commands or list_envs

    if show_info:
        print('RunCommands', __version__)

    if debug:
        printer.debug('All args:', argv)
        printer.debug('Run args:', run_argv)
        printer.debug('Command args:', command_argv)
        echo = True

    if config_file is None:
        if os.path.isfile(DEFAULT_CONFIG_FILE):
            config_file = DEFAULT_CONFIG_FILE

    options = options.copy()

    for name, value in options.items():
        if name in run_command.optionals:
            raise RunnerError(
                'Cannot pass {name} via --option; use --{option_name} instead'
                .format(name=name, option_name=name.replace('_', '-')))

    if version is not None:
        options['version'] = version

    runner = CommandRunner(
        module,
        config_file=config_file,
        env=env,
        options=options,
        echo=echo,
        hide=hide,
        debug=debug,
    )

    if print_and_exit:
        if list_envs:
            runner.print_envs()
        if list_commands:
            runner.print_usage()
    elif not command_argv:
        printer.warning('\nNo command(s) specified')
        runner.print_usage()
    else:
        runner.run(command_argv, run_args)


run_command = Command(run)


def read_run_args_from_file(parser, section):
    if isinstance(section, Command):
        name = section.name
        if name == 'runcommands':
            section = 'runcommands'
        else:
            section = 'runcommands:{name}'.format(name=name)

    if section == 'runcommands':
        sections = ['runcommands']
    elif section.startswith('runcommands:'):
        sections = ['runcommands', section]
    else:
        raise ValueError('Bad section: %s' % section)

    sections = [section for section in sections if section in parser]

    if not sections:
        return {}

    items = {}
    for section in sections:
        items.update(parser[section])

    if not items:
        return {}

    arg_map = run_command.arg_map
    arg_parser = run_command.get_arg_parser()
    option_template = '--{name}={value}'
    argv = []

    for name, value in items.items():
        option_name = '--{name}'.format(name=name)
        option = arg_map.get(option_name)

        value = value.strip()

        true_values = ('true', 't', 'yes', 'y', '1')
        false_values = ('false', 'f', 'no', 'n', '0')
        bool_values = true_values + false_values

        if option is not None:
            is_bool = option.is_bool
            if option.name == 'hide' and value not in bool_values:
                is_bool = False
            is_dict = option.is_dict
            is_list = option.is_list
        else:
            is_bool = False
            is_dict = False
            is_list = False

        if is_bool:
            true = value in true_values
            if name == 'no':
                item = '--no' if true else '--yes'
            elif name.startswith('no-'):
                option_yes_name = '--{name}'.format(name=name[3:])
                item = option_name if true else option_yes_name
            elif name == 'yes':
                item = '--yes' if true else '--no'
            else:
                option_no_name = '--no-{name}'.format(name=name)
                item = option_name if true else option_no_name
            argv.append(item)
        elif is_dict or is_list:
            values = value.splitlines()
            if len(values) == 1:
                values = values[0].split()
            values = (v.strip() for v in values)
            values = (v for v in values if v)
            argv.extend(option_template.format(name=name, value=v) for v in values)
        else:
            item = option_template.format(name=name, value=value)
            argv.append(item)

    args, remaining = arg_parser.parse_known_args(argv)

    if remaining:
        raise RunCommandsError('Unknown args read from setup.cfg: %s' % ' '.join(remaining))

    return vars(args)


def make_run_args_config_parser():
    file_names = ('runcommands.cfg', 'setup.cfg')

    config_parser = ConfigParser(empty_lines_in_values=False)
    config_parser.optionxform = lambda s: s

    for file_name in file_names:
        if os.path.isfile(file_name):
            with open(file_name) as config_parser_fp:
                config_parser.read_file(config_parser_fp)
            break

    return config_parser


def partition_argv(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        return argv, [], []

    if '--' in argv:
        i = argv.index('--')
        return argv, argv[:i], argv[i + 1:]

    run_argv = []
    option = None
    arg_map = run_command.arg_map
    parser = run_command.get_arg_parser()
    parse_optional = parser._parse_optional

    for i, arg in enumerate(argv):
        option_data = parse_optional(arg)
        if option_data is not None:
            # Arg looks like an option (according to argparse).
            action, name, value = option_data
            if name not in arg_map:
                # Unknown option.
                break
            run_argv.append(arg)
            if value is None:
                # The option's value will be expected on the next pass.
                option = arg_map[name]
            else:
                # A value was supplied with -nVALUE, -n=VALUE, or
                # --name=VALUE.
                option = None
        elif option is not None:
            choices = action.choices or ()
            if option.takes_value:
                run_argv.append(arg)
                option = None
            elif arg in choices or hasattr(choices, arg):
                run_argv.append(arg)
                option = None
            else:
                # Unexpected option value
                break
        else:
            # The first arg doesn't look like an option (it's probably
            # a command name).
            break
    else:
        # All args were consumed by command; none remain.
        i += 1

    remaining = argv[i:]

    return argv, run_argv, remaining