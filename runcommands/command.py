import argparse
import inspect
import signal
import sys
import time
from collections import OrderedDict
from inspect import Parameter
from typing import Mapping

from .args import POSITIONAL_PLACEHOLDER, Arg, ArgConfig, HelpArg
from .exc import CommandError, RunCommandsError
from .util import cached_property, camel_to_underscore, get_hr, printer


__all__ = ['command', 'Command']


class Command:

    """Wraps a callable and provides a command line argument parser.

    Args:
        implementation (callable): A callable that implements the
            command's functionality. The command's console script will
            be generated by inspecting this callable.
        name (str): Name of command as it will be called from the
            command line. Defaults to ``implementation.__name__`` (with
            underscores replaced with dashes).
        description (str): Description of command shown in command
            help. Defaults to ``implementation.__doc__``.
        timed (bool): Whether the command should be timed. Will print an
            info message showing how long the command took to complete
            when ``True``. Defaults to ``False``.
        arg_config (dict): For commands defined as classes, this can be
            used to configure common base args instead of repeating the
            configuration for each subclass. Note that its keys should
            be actual parameter names and not normalized arg names.
        debug (bool): When this is set, additional debugging info will
            be shown.

    This is typically used via the :func:`command` decorator::

        from runcommands import command

        @command
        def my_command():
            ...

    Decorating a function with :func:`command` will create an instance
    of :class:`Command` with the wrapped function as its implementation.

    Args can be passed to :func:`command`, which will be passed through
    to the :class:`Command` constructor::

        @command(name='better-name')
        def my_command():
            ...

    It's also possible to use a class directly as a command::

        @command
        class MyCommand(Command):

            def implementation(self):
                ...

    Using the :func:`command` decorator on a class will create an
    instance of the class in the namespace where the class is defined.

    Command Names:

    A command's name is derived from the normalized name of its
    implementation function by default::

        @command
        def some_command():
            ...

        # Command name: some-command

    A name can be set explicitly instead, in which case it *won't* be
    normalized::

        @command(name='do_stuff')
        def some_command():
            ...

        # Command name: do_stuff

    If the command is defined as a class, its name will be derived from
    its class name by default (split into words then normalized)::

        class SomeCommand(Command):
            ...

        # Command name: some-command

    The `command` decorator or a class-level attribute can be used to
    set the command's name explicitly::

        @command(name='do_stuff')
        class SomeCommand(Command):
            ...

        class SomeCommand(Command):
            name = 'do_stuff'

        # Command name in both cases: do_stuff

    """

    def __init__(self, implementation=None, name=None, description=None, base_command=None,
                 timed=False, arg_config=None, default_args=None, debug=False):
        if implementation is None:
            if not hasattr(self, 'implementation'):
                raise CommandError(
                    'Missing implementation; it must be passed in as a function or defined as a '
                    'method on the command class')
            default_name = self.__class__.__name__
        else:
            self.implementation = implementation
            default_name = implementation.__name__

        name = name or getattr(self.__class__, 'name', None) or self.normalize_name(default_name)

        is_subcommand = base_command is not None

        if is_subcommand:
            name = ':'.join((base_command.name, name))

        description = description or self.get_description_from_docstring(self.implementation)
        short_description = description.splitlines()[0] if description else None

        self.name = name
        self.description = description
        self.short_description = short_description
        self.timed = timed
        self.arg_config = arg_config or {}
        self.debug = debug
        self.default_args = default_args or {}
        self.mutual_exclusion_groups = {}

        # Subcommand-related attributes
        first_arg = next(iter(self.args.values()), None)
        self.base_command = base_command
        self.is_subcommand = is_subcommand
        self.subcommands = []
        self.first_arg = first_arg
        self.first_arg_has_choices = False if first_arg is None else bool(first_arg.choices)

        if is_subcommand:
            base_command.add_subcommand(self)

    @classmethod
    def command(cls, name=None, description=None, base_command=None, timed=False):
        args = dict(description=description, base_command=base_command, timed=timed)

        if isinstance(name, type):
            # Bare class decorator
            name.implementation.__name__ = camel_to_underscore(name.__name__)
            return name(**args)

        if callable(name):
            # Bare function decorator
            return cls(implementation=name, **args)

        def wrapper(wrapped):
            if isinstance(wrapped, type):
                wrapped.implementation.__name__ = camel_to_underscore(wrapped.__name__)
                return wrapped(name=name, **args)
            return cls(implementation=wrapped, name=name, **args)

        return wrapper

    @classmethod
    def subcommand(cls, base_command, name=None, description=None, timed=False):
        """Create a subcommand of the specified base command."""
        return cls.command(name, description, base_command, timed)

    @property
    def is_base_command(self):
        return bool(self.subcommands)

    @property
    def subcommand_depth(self):
        depth = 0
        base_command = self.base_command
        while base_command:
            depth += 1
            base_command = base_command.base_command
        return depth

    @cached_property
    def base_name(self):
        if self.is_subcommand:
            return self.name.split(':', self.subcommand_depth)[-1]
        return self.name

    @cached_property
    def prog_name(self):
        if self.is_subcommand:
            return ' '.join(self.name.split(':', self.subcommand_depth))
        return self.base_name

    def add_subcommand(self, subcommand):
        name = subcommand.base_name
        self.subcommands.append(subcommand)
        if not self.first_arg_has_choices:
            if self.first_arg.choices is None:
                self.first_arg.choices = []
            self.first_arg.choices.append(name)

    def get_description_from_docstring(self, implementation):
        description = implementation.__doc__
        if description is not None:
            description = description.strip() or None
        if description is not None:
            lines = description.splitlines()
            title = lines[0]
            if title.endswith('.'):
                title = title[:-1]
            lines = [title] + [line[4:] for line in lines[1:]]
            description = '\n'.join(lines)
        return description

    def run(self, argv=None, **overrides):
        if self.timed:
            start_time = time.monotonic()

        argv = sys.argv[1:] if argv is None else argv
        kwargs = argv if isinstance(argv, dict) else self.parse_args(argv)
        kwargs.update(overrides)

        args = []
        for arg in self.args.values():
            name = arg.parameter.name
            if arg.is_positional:
                if name in kwargs:
                    value = kwargs.pop(name)
                    args.append(value)
                else:
                    args.append(POSITIONAL_PLACEHOLDER)
            elif arg.is_var_positional:
                if name in kwargs:
                    value = kwargs.pop(name)
                    args.extend(value)

        result = self(*args, **kwargs)

        if self.timed:
            self.print_elapsed_time(time.monotonic() - start_time)

        return result

    def console_script(self, argv=None, **overrides):
        debug = self.debug
        argv = sys.argv[1:] if argv is None else argv
        base_argv = argv
        is_base_command = self.is_base_command
        found_subcommand = None

        if hasattr(self, 'sigint_handler'):
            signal.signal(signal.SIGINT, self.sigint_handler)

        if is_base_command:
            base_argv = []
            subcommand_map = {sub.name: sub for sub in self.subcommands}
            for i, arg in enumerate(argv):
                if arg.startswith(':'):
                    arg = arg[1:]
                    base_argv.append(arg)
                else:
                    qualified_name = '{self.name}:{arg}'.format_map(locals())
                    if qualified_name in subcommand_map:
                        found_subcommand = subcommand_map[qualified_name]
                        base_argv.append(arg)
                        subcommand_argv = argv[i + 1:]
                        if debug:
                            printer.debug('Found subcommand:', found_subcommand.name)
                        break
                    else:
                        base_argv.append(arg)

        try:
            result = self.run(base_argv, **overrides)
        except RunCommandsError as result:
            if debug:
                raise
            return_code = result.return_code if hasattr(result, 'return_code') else 1
            result_str = str(result)
            if result_str:
                if return_code:
                    printer.error(result_str, file=sys.stderr)
                else:
                    printer.print(result_str)
        else:
            return_code = result.return_code if hasattr(result, 'return_code') else 0

        if found_subcommand:
            return found_subcommand.console_script(subcommand_argv)

        return return_code

    def __call__(self, *args, **kwargs):
        arguments = self.args
        default_args = self.default_args

        defaults = {}

        num_args = len(args)
        new_args = []
        new_kwargs = kwargs.copy()
        missing_positionals = []

        for i, arg in enumerate(arguments.values()):
            name = arg.parameter.name
            if arg.is_positional:
                if i < num_args:
                    value = args[i]
                    if value is POSITIONAL_PLACEHOLDER:
                        if name in default_args:
                            value = default_args[name]
                        else:
                            value = arg.default
                elif name in default_args:
                    value = default_args[name]
                else:
                    value = Parameter.empty
                if value is Parameter.empty:
                    missing_positionals.append(arg.name)
                else:
                    new_args.append(value)
            elif arg.is_var_positional:
                value = args[i:]
                new_args.extend(value)
            elif name in kwargs:
                value = kwargs[name]
                new_kwargs[name] = value
            elif name in default_args:
                value = default_args[name]
                new_kwargs[name] = value
                defaults[name] = value

        if missing_positionals:
            count = len(missing_positionals)
            ess = '' if count == 1 else 's'
            verb = 'was' if count == 1 else 'were'
            missing = ', '.join(missing_positionals)
            message = (
                '{count} positional arg{ess} {verb}n\'t passed to the {self.name} command '
                '(and no default{ess} {verb} set): {missing}')
            message = message.format_map(locals())
            raise CommandError(message)

        if self.debug:
            printer.debug('Command called:', self.name)
            printer.debug('    Received positional args:', args)
            printer.debug('    Received keyword args:', kwargs)
            if defaults:
                printer.debug('    Added default args:', ', '.join(defaults))

        if self.debug:
            printer.debug('Running command:', self.name)
            printer.debug('    Final positional args:', new_args)
            printer.debug('    Final keyword args:', new_kwargs)

        return self.implementation(*new_args, **new_kwargs)

    def parse_args(self, argv):
        if self.debug:
            printer.debug('Parsing args for command `{self.name}`: {argv}'.format_map(locals()))
        argv = self.expand_short_options(argv)
        parsed_args = self.arg_parser.parse_args(argv)
        parsed_args = vars(parsed_args)
        for k, v in parsed_args.items():
            if v == '':
                parsed_args[k] = None
        return parsed_args

    def parse_optional(self, string):
        """Parse string into name, option, and value (if possible).

        If the string is a known option name, the string, the
        corresponding option, and ``None`` will be returned.

        If the string has the form ``--option=<value>`` or
        ``-o=<value>``, it will be split on equals into an option name
        and value. If the option name is known, the option name, the
        corresponding option, and the value will be returned.

        In all other cases, ``None`` will be returned to indicate that
        the string doesn't correspond to a known option.

        """
        option_map = self.option_map
        if string in option_map:
            return string, option_map[string], None
        if '=' in string:
            name, value = string.split('=', 1)
            if name in option_map:
                return name, option_map[name], value
        return None

    def expand_short_options(self, argv):
        """Convert grouped short options like `-abc` to `-a, -b, -c`.

        This is necessary because we set ``allow_abbrev=False`` on the
        ``ArgumentParser`` in :attr:`self.arg_parser`. The argparse docs
        say ``allow_abbrev`` applies only to long options, but it also
        affects whether short options grouped behind a single dash will
        be parsed into multiple short options.

        """
        if self.debug:
            has_multi_short_options = False
            printer.debug('Expanding short options for `{self.name}`: {argv}'.format_map(locals()))
        debug = self.debug
        parse_multi_short_option = self.parse_multi_short_option
        new_argv = []
        for i, arg in enumerate(argv):
            result, is_multi_short_option = parse_multi_short_option(arg)
            if debug:
                has_multi_short_options = has_multi_short_options or is_multi_short_option
                if is_multi_short_option:
                    printer.debug('    Found multi short option:', arg, '=>', result)
            if arg == '--':
                new_argv.extend(argv[i:])
                break
            new_argv.extend(result)
        if debug and not has_multi_short_options:
            printer.debug('    No mult short options found')
        return new_argv

    def parse_multi_short_option(self, arg):
        """Parse args like '-xyz' into ['-x', '-y', '-z'].

        Returns the arg, parsed or not, in a list along with a flag to
        indicate whether arg is a multi short option.

        For example::

            '-a' -> ['-a'], False
            '-xyz' -> ['-x', '-y', '-z'], True

        """
        if len(arg) < 3 or arg[0] != '-' or arg[1] == '-' or arg[2] == '=':
            # Not a multi short option like '-abc'.
            return [arg], False
        # Appears to be a multi short option.
        return ['-{a}'.format(a=a) for a in arg[1:]], True

    def normalize_name(self, name):
        name = camel_to_underscore(name)
        # Chomp a single trailing underscore *if* the name ends with
        # just one trailing underscore. This accommodates the convention
        # of adding a trailing underscore to reserved/built-in names.
        if name.endswith('_'):
            if name[-2] != '_':
                name = name[:-1]
        name = name.replace('_', '-')
        name = name.lower()
        return name

    def find_arg(self, name):
        """Find arg by normalized arg name or parameter name."""
        name = self.normalize_name(name)
        return self.args.get(name)

    def find_parameter(self, name):
        """Find parameter by name or normalized arg name."""
        name = self.normalize_name(name)
        arg = self.args.get(name)
        return None if arg is None else arg.parameter

    def get_arg_config(self, param):
        annotation = param.annotation
        if annotation is param.empty:
            annotation = self.arg_config.get(param.name) or ArgConfig()
        elif isinstance(annotation, type):
            annotation = ArgConfig(type=annotation)
        elif isinstance(annotation, str):
            annotation = ArgConfig(help=annotation)
        elif isinstance(annotation, Mapping):
            annotation = ArgConfig(**annotation)
        return annotation

    def get_short_option_for_arg(self, name, names, used):
        first_char = name[0]
        first_char_upper = first_char.upper()

        if name == 'help':
            candidates = (first_char,)
        elif name.startswith('h'):
            candidates = (first_char_upper,)
        else:
            candidates = (first_char, first_char_upper)

        for char in candidates:
            short_option = '-{char}'.format_map(locals())
            if short_option not in used:
                return short_option

    def get_long_option_for_arg(self, name):
        return '--{name}'.format_map(locals())

    def get_inverse_option_for_arg(self, long_option):
        if long_option == '--yes':
            return '--no'
        if long_option == '--no':
            return '--yes'
        if long_option.startswith('--no-'):
            return long_option.replace('--no-', '--', 1)
        return long_option.replace('--', '--no-', 1)

    def print_elapsed_time(self, elapsed_time):
        m, s = divmod(elapsed_time, 60)
        m = int(m)
        hr = get_hr()
        msg = '{hr}\nElapsed time for {self.name} command: {m:d}m {s:.3f}s\n{hr}'
        msg = msg.format_map(locals())
        printer.info(msg)

    @cached_property
    def parameters(self):
        implementation = self.implementation
        signature = inspect.signature(implementation)
        params = tuple(signature.parameters.items())
        params = OrderedDict(params)
        return params

    @cached_property
    def has_kwargs(self):
        return any(p.kind is p.VAR_KEYWORD for p in self.parameters.values())

    @cached_property
    def args(self):
        """Create args from function parameters."""
        params = self.parameters
        args = OrderedDict()

        normalize_name = self.normalize_name
        get_arg_config = self.get_arg_config
        get_short_option = self.get_short_option_for_arg
        get_long_option = self.get_long_option_for_arg
        get_inverse_option = self.get_inverse_option_for_arg

        names = {normalize_name(name) for name in params}

        used_short_options = set()
        for param in params.values():
            annotation = get_arg_config(param)
            short_option = annotation.short_option
            if short_option:
                used_short_options.add(short_option)

        for name, param in params.items():
            empty = param.empty
            name = normalize_name(name)

            skip = (
                name.startswith('_') or
                param.kind is param.VAR_KEYWORD or
                param.kind is param.KEYWORD_ONLY)
            if skip:
                continue

            annotation = get_arg_config(param)
            container = annotation.container
            type = annotation.type
            choices = annotation.choices
            help = annotation.help
            inverse_help = annotation.inverse_help
            short_option = annotation.short_option
            long_option = annotation.long_option
            inverse_option = annotation.inverse_option
            action = annotation.action
            nargs = annotation.nargs
            mutual_exclusion_group = annotation.mutual_exclusion_group

            default = param.default
            is_var_positional = param.kind is param.VAR_POSITIONAL
            is_positional = default is empty and not is_var_positional

            if annotation.default is not empty:
                if is_positional:
                    default = annotation.default
                else:
                    message = (
                        'Got default for `{self.name}` command\'s optional arg `{name}` via '
                        'arg annotation. Optional args must specify their defaults via keyword '
                        'arg values.'
                    ).format_map(locals())
                    raise CommandError(message)

            if not (is_positional or is_var_positional):
                if not short_option:
                    short_option = get_short_option(name, names, used_short_options)
                    used_short_options.add(short_option)
                if not long_option:
                    long_option = get_long_option(name)
                if not inverse_option:
                    # NOTE: The DISABLE marker evaluates as True
                    inverse_option = get_inverse_option(long_option)

            args[name] = Arg(
                command=self,
                parameter=param,
                name=name,
                container=container,
                type=type,
                positional=is_positional,
                default=default,
                choices=choices,
                help=help,
                inverse_help=inverse_help,
                short_option=short_option,
                long_option=long_option,
                inverse_option=inverse_option,
                action=action,
                nargs=nargs,
                mutual_exclusion_group=mutual_exclusion_group,
            )

        if 'help' not in args:
            args['help'] = HelpArg(command=self)

        option_map = OrderedDict()
        for arg in args.values():
            for option in arg.options:
                option_map.setdefault(option, [])
                option_map[option].append(arg)

        for option, option_args in option_map.items():
            if len(option_args) > 1:
                names = ', '.join(a.parameter.name for a in option_args)
                message = (
                    'Option {option} of command {self.name} maps to multiple parameters: {names}')
                message = message.format_map(locals())
                raise CommandError(message)

        return args

    @cached_property
    def arg_parser(self):
        use_default_help = isinstance(self.args['help'], HelpArg)

        parser = argparse.ArgumentParser(
            prog=self.prog_name,
            description=self.description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            argument_default=argparse.SUPPRESS,
            add_help=use_default_help,
            allow_abbrev=False,  # See note in `self.parse_args()`
        )

        default_args = self.default_args

        for name, arg in self.args.items():
            if name == 'help' and use_default_help:
                continue

            param = arg.parameter
            options, kwargs = arg.add_argument_args

            if arg.is_positional and param.name in default_args:
                # Positionals are made optional if a default value is
                # specified via config.
                kwargs = kwargs.copy()
                kwargs['nargs'] = '*' if arg.container else '?'

            mutual_exclusion_group_name = arg.mutual_exclusion_group
            if mutual_exclusion_group_name:
                if mutual_exclusion_group_name not in self.mutual_exclusion_groups:
                    self.mutual_exclusion_groups[mutual_exclusion_group_name] = \
                        parser.add_mutually_exclusive_group()
                mutual_exclusion_group = self.mutual_exclusion_groups[mutual_exclusion_group_name]
                mutual_exclusion_group.add_argument(*options, **kwargs)
            else:
                parser.add_argument(*options, **kwargs)

            inverse_args = arg.add_argument_inverse_args
            if inverse_args is not None:
                options, kwargs = inverse_args
                parser.add_argument(*options, **kwargs)

        return parser

    @cached_property
    def positionals(self):
        args = self.args.items()
        return OrderedDict((name, arg) for (name, arg) in args if arg.is_positional)

    @cached_property
    def var_positional(self):
        args = self.args.items()
        for name, arg in args:
            if arg.is_var_positional:
                return arg
        return None

    @cached_property
    def optionals(self):
        args = self.args.items()
        return OrderedDict((name, arg) for (name, arg) in args if arg.is_optional)

    @cached_property
    def option_map(self):
        """Map command-line options to args."""
        option_map = OrderedDict()
        for arg in self.args.values():
            for option in arg.options:
                option_map[option] = arg
        return option_map

    @property
    def help(self):
        help_ = self.arg_parser.format_help()
        help_ = help_.split(': ', 1)[1]
        help_ = help_.strip()
        return help_

    @property
    def usage(self):
        usage = self.arg_parser.format_usage()
        usage = usage.split(': ', 1)[1]
        usage = usage.strip()
        return usage

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.usage

    def __repr__(self):
        return 'Command(name={self.name})'.format(self=self)


command = Command.command
subcommand = Command.subcommand
