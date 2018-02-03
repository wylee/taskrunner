import argparse
import builtins
import inspect
import itertools
import re
import sys
import time
from collections import OrderedDict
from enum import Enum

from .const import DEFAULT_ENV
from .exc import CommandError, RunCommandsError
from .util import Hide, cached_property, get_hr, printer


__all__ = ['command']


class Command:

    """Command.

    Wraps a callable and provides a command line argument parser.

    This is typically used via the ``command`` decorator::

        from runcommands import command

        @command
        def my_command(config):
            pass

    Args:
        implementation (callable): A callable that implements the
            command's functionality. The command's console script will
            be generated by inspecting this callable.
        name (str): Name of command as it will be called from the
            command line. Defaults to ``implementation.__name__`` (with
            underscores replaced with dashes).
        description (str): Description of command shown in command
            help. Defaults to ``implementation.__doc__``.
        env (str): Env to run command in. If this is specified, the
            command *will* be run in this env and may *only* be run in
            this env. If this is set to :const:`DEFAULT_ENV`, the
            command will be run in the configured default env.
        default_env (str): Default env to run command in. If this is
            specified, the command will be run in this env by default
            and may also be run in any other env. If this is set to
            :const:`DEFAULT_ENV`, the command will be run in the
            configured default env.
        config (dict): Additional or override config. This will
            supplement or override config read from other sources.
            Passed args take precedence over this config.
            ``{'dotted.name': value}``
        timed (bool): Whether the command should be timed. Will print an
            info message showing how long the command took to complete
            when ``True``. Defaults to ``False``.

    """

    def __init__(self, implementation, name=None, description=None, env=None, default_env=None,
                 config=None, timed=False):
        if env is not None and default_env is not None:
            raise CommandError('Only one of `env` or `default_env` may be specified')

        self.implementation = implementation
        self.name = name or self.normalize_name(implementation.__name__)
        self.description = description or self.get_description_from_docstring(implementation)
        self.env = env
        self.default_env = default_env
        self.config = config or {}
        self.timed = timed

        # Keep track of used short option names so that the same name
        # isn't used more than once.
        self.used_short_options = {}
        qualified_name = '{implementation.__module__}.{implementation.__qualname__}'
        qualified_name = qualified_name.format_map(locals())
        self.qualified_name = qualified_name
        self.defaults_path = 'defaults.{self.qualified_name}'.format_map(locals())
        self.short_defaults_path = 'defaults.{self.name}'.format_map(locals())

    @classmethod
    def command(cls, name=None, description=None, env=None, default_env=None,
                config=None, timed=False):
        args = dict(
            description=description,
            env=env,
            default_env=default_env,
            config=config,
            timed=timed,
        )

        if callable(name):
            # @command used as a bare decorator.
            return Command(implementation=name, **args)

        def wrapper(wrapped):
            return Command(implementation=wrapped, name=name, **args)

        return wrapper

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

    def get_run_env(self, specified_env, global_default_env):
        env = self.env
        default_env = self.default_env

        if env is not None:
            if env is DEFAULT_ENV:
                env = global_default_env

            if env is True:
                # The command has no default env and requires one to be
                # specified.
                if not specified_env:
                    raise CommandError(
                        'The `{self.name}` command requires an env to be specified'
                        .format_map(locals()))
                run_env = specified_env
            elif env is False:
                # The command explicitly doesn't run in an env.
                if specified_env:
                    raise CommandError(
                        'The `{self.name}` command may *not* be run in an env but '
                        'the "{specified_env}" env was specified'.format_map(locals()))
                run_env = None
            else:
                # The command may only be run in a designated env; make
                # sure the specified env matches that env.
                if specified_env and specified_env != env:
                    raise CommandError(
                        'The `{self.name}` command may be run only in the "{env}" env but '
                        'the "{specified_env}" env was specified'.format_map(locals()))
                run_env = env
        elif default_env is not None:
            if specified_env:
                # If an env was specified, the command will be run in that env.
                run_env = specified_env
            elif default_env is True or default_env is DEFAULT_ENV:
                # If no env was specified *and* the command indicates that it
                # should be run in the global default env, the command will be
                # run in the global default env.
                run_env = global_default_env
            else:
                # Otherwise, the command will be run in whatever default env
                # was indicated in the command definition.
                run_env = default_env
        else:
            # The command was configured without any env options, so use
            # the specified env (which may be None).
            run_env = specified_env

        return run_env

    def run(self, run_config, argv, **kwargs):
        if self.timed:
            start_time = time.monotonic()

        run_env = self.get_run_env(run_config.env, run_config.default_env)
        run_config = run_config.copy(env=run_env)

        config = Config(run=run_config, **self.config.copy())

        all_args = self.parse_args(config, argv)
        all_args.update(kwargs)
        result = self(config, **all_args)

        if self.timed:
            hide = kwargs.get('hide', config.run.hide)
            if not Hide.hide_stdout(hide):
                self.print_elapsed_time(time.monotonic() - start_time)

        return result

    def console_script(self, _argv=None, _run_args=None, **kwargs):
        from .run import read_run_args

        argv = sys.argv[1:] if _argv is None else _argv

        try:
            run_config = RunConfig(commands={self.name: self})
            run_config.update(read_run_args(self))
            run_config.update(_run_args or {})
            self.run(run_config, argv, **kwargs)
        except RunCommandsError as exc:
            printer.error(exc, file=sys.stderr)
            return 1

        return 0

    def __call__(self, config, *args, **kwargs):
        # Merge config from the command's definition along with options
        # specified on the command line. We already do this in the run()
        # method above, but we have to ensure it's done when the command
        # is called directly too.
        config = config.copy(self.config.copy())
        debug = config.run.debug
        commands = config.run.commands
        replacement = commands.get(self.name)
        replaced = replacement is not None and replacement is not self

        if replaced:
            if debug:
                printer.debug('Command replaced:', self.name)
                printer.debug('    ', self.qualified_name, '=>', replacement.qualified_name)
            return replacement(config, *args, **kwargs)

        if debug:
            printer.debug('Command called:', self.name)
            printer.debug('    Received positional args:', args)
            printer.debug('    Received keyword args:', kwargs)

        params = self.parameters
        defaults = self.get_defaults(config)

        if defaults:
            nonexistent_defaults = [n for n in defaults if n not in params]
            if nonexistent_defaults:
                nonexistent_defaults = ', '.join(nonexistent_defaults)
                raise CommandError(
                    'Nonexistent default options specified for {self.name}: {nonexistent_defaults}'
                    .format_map(locals()))

            positionals = OrderedDict()
            for name, value in zip(self.positionals, args):
                positionals[name] = value

            for name in self.positionals:
                present = name in positionals or name in kwargs
                if not present and name in defaults:
                    kwargs[name] = defaults[name]

            for name in self.optionals:
                present = name in kwargs
                if not present and name in defaults:
                    kwargs[name] = defaults[name]

        def set_run_default(option):
            # If all of the following are true, the global default value
            # for the option will be injected into the options passed to
            # the command for this run:
            #
            # - This command defines the option.
            # - The option was not passed explicitly on this run.
            # - A global default is set for the option (it's not None).
            if option in params and option not in kwargs:
                global_default = config._get_dotted('run.%s' % option, None)
                if global_default is not None:
                    kwargs[option] = global_default

        set_run_default('echo')
        set_run_default('hide')

        if debug:
            printer.debug('Running command:', self.name)
            printer.debug('    Final positional args:', repr(args))
            printer.debug('    Final keyword args:', repr(kwargs))

        return self.implementation(config, *args, **kwargs)

    def get_defaults(self, config):
        defaults = config._get_dotted(self.defaults_path, RawConfig())
        defaults.update(config._get_dotted(self.short_defaults_path, RawConfig()))
        return defaults

    def get_default(self, config, name, default=None):
        defaults = self.get_defaults(config)
        return defaults.get(name, default)

    def parse_args(self, config, argv):
        debug = config.run.debug
        if debug:
            printer.debug('Parsing args for command `{self.name}`: {argv}'.format_map(locals()))

        parsed_args = self.get_arg_parser(config).parse_args(argv)
        parsed_args = vars(parsed_args)
        for k, v in parsed_args.items():
            if v == '':
                parsed_args[k] = None
        return parsed_args

    def normalize_name(self, name):
        # Chomp a single trailing underscore *if* the name ends with
        # just one trailing underscore. This accommodates the convention
        # of adding a trailing underscore to reserved/built-in names.
        if name.endswith('_'):
            if name[-2] != '_':
                name = name[:-1]

        name = name.replace('_', '-')
        name = name.lower()
        return name

    def arg_names_for_param(self, param):
        params = self.parameters
        param = params[param] if isinstance(param, str) else param
        name = param.name

        if name.startswith('_'):
            return []

        if param.is_positional:
            return [param.real_name]
        elif param.is_keyword_only:
            return []

        arg_names = []

        if param.short_option:
            # Short option specified manually.
            short_name = param.short_option
            used_for = self.used_short_options.get(short_name)
            if used_for and used_for != name:
                message = 'Short option {short_name} already used for option {used_for}'
                message = message.format_map(locals())
                raise CommandError()
            arg_names.append(short_name)
            self.used_short_options[short_name] = param.name
        else:
            # Automatically select short option.
            first_char = name[0]
            first_char_upper = first_char.upper()

            if first_char == 'e':
                # Ensure echo gets -E for consistency.
                if name == 'echo':
                    candidates = ('E',)
                elif 'echo' not in params:
                    candidates = (first_char, first_char_upper)
                else:
                    candidates = ()
            elif first_char == 'h':
                # Ensure help gets -h and hide gets -H for consistency.
                if name == 'help':
                    candidates = (first_char,)
                elif name == 'hide':
                    candidates = (first_char_upper,)
                elif 'hide' not in params:
                    candidates = (first_char_upper,)
                else:
                    candidates = ()
            else:
                candidates = (first_char, first_char_upper)

            for char in candidates:
                short_name = '-{char}'.format_map(locals())
                used_for = self.used_short_options.get(short_name)
                if not used_for or used_for == name:
                    arg_names.append(short_name)
                    self.used_short_options[short_name] = param.name
                    break

        long_name = '--{name}'.format_map(locals())
        arg_names.append(long_name)

        default_no_long_name = '--no-{name}'.format_map(locals())
        if param.is_bool_or:
            arg_names.append(default_no_long_name)
        elif param.is_bool:
            if name == 'yes':
                no_long_name = '--no'
            elif name == 'no':
                no_long_name = '--yes'
            else:
                no_long_name = default_no_long_name
            if not isinstance(param, HelpParameter):
                arg_names.append(no_long_name)

        return arg_names

    def print_elapsed_time(self, elapsed_time):
        m, s = divmod(elapsed_time, 60)
        m = int(m)
        hr = get_hr()
        msg = '{hr}\nElapsed time for {self.name} command: {m:d}m {s:.3f}s\n{hr}'
        msg = msg.format_map(locals())
        printer.info(msg)

    @cached_property
    def signature(self):
        return inspect.signature(self.implementation)

    @cached_property
    def parameters(self):
        parameters = tuple(self.signature.parameters.items())[1:]
        params = OrderedDict()

        # This will be overridden if the command explicitly defines an
        # arg named help.
        params['help'] = HelpParameter()

        position = 1
        for name, param in parameters:
            if not name.startswith('_'):
                name = self.normalize_name(name)
            if param.default is param.empty:
                param_position = position
                position += 1
            else:
                param_position = None
            params[name] = Parameter(name, param, param_position)

        return params

    @cached_property
    def positionals(self):
        parameters = self.parameters.items()
        return OrderedDict((n, p) for (n, p) in parameters if p.is_positional)

    @cached_property
    def optionals(self):
        parameters = self.parameters.items()
        return OrderedDict((n, p) for (n, p) in parameters if p.is_optional)

    @cached_property
    def arg_map(self):
        """Map command-line arg names to parameters."""
        params = self.parameters
        param_map = self.param_map
        arg_map = OrderedDict()
        for name, arg_names in param_map.items():
            param = params[name]
            for arg_name in arg_names:
                arg_map[arg_name] = param
        return arg_map

    @cached_property
    def param_map(self):
        """Map parameter names to command-line arg names."""
        params = self.parameters
        param_map = OrderedDict((name, []) for name in params)

        params_with_short_option = (p for p in params.values() if p.short_option)
        params_without_short_option = (p for p in params.values() if not p.short_option)

        for param in itertools.chain(params_with_short_option, params_without_short_option):
            arg_names = self.arg_names_for_param(param)
            if arg_names:
                param_map[param.name] = arg_names

        return param_map

    def get_arg_parser(self, config=None):
        if config is None:
            config = Config()

        use_default_help = isinstance(self.parameters['help'], HelpParameter)

        parser = argparse.ArgumentParser(
            prog=self.name,
            description=self.description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            argument_default=argparse.SUPPRESS,
            add_help=use_default_help,
            allow_abbrev=False,
        )

        defaults = self.get_defaults(config)

        for name, arg_names in self.param_map.items():
            if not arg_names:
                continue

            if name == 'help' and use_default_help:
                continue

            param = self.parameters[name]

            if param.is_positional and name in defaults:
                default = defaults[name]
            else:
                default = param.default

            kwargs = {
                'help': param.help,
            }

            metavar = name.upper().replace('-', '_')
            if (param.is_dict or param.is_list) and len(name) > 1 and name.endswith('s'):
                metavar = metavar[:-1]

            if param.is_positional:
                kwargs['type'] = param.type
                if param.choices is not None:
                    kwargs['choices'] = param.choices
                # Make positionals optional if a default value is
                # specified via config.
                if default is not param.empty:
                    kwargs['nargs'] = '?'
                    kwargs['default'] = default
                kwargs['metavar'] = metavar
                parser.add_argument(*arg_names, **kwargs)
            else:
                kwargs['dest'] = param.real_name

                if param.is_bool_or:
                    # Allow --xyz or --xyz=<value>
                    other_type = param.type.type
                    true_or_value_kwargs = kwargs.copy()
                    true_or_value_kwargs['type'] = other_type
                    if param.choices is not None:
                        true_or_value_kwargs['choices'] = param.choices
                    true_or_value_kwargs['action'] = BoolOrAction
                    true_or_value_kwargs['nargs'] = '?'
                    true_or_value_kwargs['metavar'] = metavar
                    true_or_value_arg_names = arg_names[:-1]
                    parser.add_argument(*true_or_value_arg_names, **true_or_value_kwargs)

                    # Allow --no-xyz
                    false_kwargs = kwargs.copy()
                    parser.add_argument(arg_names[-1], action='store_false', **false_kwargs)
                elif param.is_bool:
                    parser.add_argument(*arg_names[:-1], action='store_true', **kwargs)
                    parser.add_argument(arg_names[-1], action='store_false', **kwargs)
                elif param.is_dict:
                    kwargs['action'] = DictAddAction
                    kwargs['metavar'] = metavar
                    parser.add_argument(*arg_names, **kwargs)
                elif param.is_list:
                    kwargs['action'] = ListAppendAction
                    kwargs['metavar'] = metavar
                    parser.add_argument(*arg_names, **kwargs)
                else:
                    kwargs['type'] = param.type
                    if param.choices is not None:
                        kwargs['choices'] = param.choices
                    kwargs['metavar'] = metavar
                    parser.add_argument(*arg_names, **kwargs)

        return parser

    @property
    def help(self):
        help_ = self.get_arg_parser().format_help()
        help_ = help_.split(': ', 1)[1]
        help_ = help_.strip()
        return help_

    @property
    def usage(self):
        usage = self.get_arg_parser().format_usage()
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


class Arg(dict):

    """Configuration for an arg.

    This can be used as a function parameter annotation to explicitly
    configure an arg, overriding default behavior.

    Args:

        short_option (str): A short option like -x to use instead of the
            default, which is derived from the first character of the
            arg name.
        type (type): The type of the arg. By default, a positional arg
            will be parsed as str and an optional/keyword args will be
            parsed as the type of its default value (or as str if the
            default value is None).
        choices (sequence): A sequence of allowed choices for the arg.
        help (str): The help string for the arg.

    .. note:: For convenience, regular dicts can be used to annotate
        args instead instead; they will be converted to instances of
        this class automatically so they

    """

    short_option_regex = re.compile(r'^-\w$')

    def __init__(self, *, short_option=None, type=None, choices=None, help=None):
        if short_option is not None:
            if not self.short_option_regex.search(short_option):
                message = 'Expected short option with form -x, not "{short_option}"'
                message = message.format_map(locals())
                raise ValueError(message)
        if type is not None:
            if not isinstance(type, builtins.type):
                message = 'Expected type, not {type.__class__.__name__}'.format_map(locals())
                raise ValueError(message)
        super().__init__(short_option=short_option, type=type, choices=choices, help=help)


class Parameter:

    def __init__(self, name, parameter, position):
        default = parameter.default
        empty = parameter.empty
        annotation = parameter.annotation

        if annotation is empty:
            annotation = Arg()
        elif isinstance(annotation, type):
            annotation = Arg(type=annotation)
        elif isinstance(annotation, str):
            annotation = Arg(help=annotation)
        elif not isinstance(annotation, Arg):
            # Assume dict or other mapping.
            annotation = Arg(**annotation)

        self._parameter = parameter
        self.name = name
        self.real_name = parameter.name
        self.is_positional = default is empty
        self.is_optional = not self.is_positional
        self.is_keyword_only = self.kind is parameter.KEYWORD_ONLY
        self.annotation = annotation
        self.short_option = annotation['short_option']

        arg_type = annotation.get('type')

        if arg_type is None:
            if name == 'hide' and self.is_optional:
                self.type = bool_or(Hide)
                self.is_bool = False
                self.is_dict = False
                self.is_enum = False
                self.is_list = False
                self.is_bool_or = True
            else:
                self.type = str if default in (None, empty) else type(default)
                self.is_bool = isinstance(default, bool)
                self.is_dict = isinstance(default, dict)
                self.is_enum = isinstance(default, Enum)
                self.is_list = isinstance(default, (list, tuple))
                self.is_bool_or = False
        else:
            if not isinstance(arg_type, type):
                message = 'Expect type, not {arg_type.__class__.__name__}'.format_map(locals())
                raise TypeError(message)
            self.type = arg_type
            self.is_bool = issubclass(arg_type, bool)
            self.is_dict = issubclass(arg_type, dict)
            self.is_enum = issubclass(arg_type, Enum)
            self.is_list = issubclass(arg_type, (list, tuple))
            self.is_bool_or = issubclass(arg_type, bool_or)

        choices = annotation.get('choices')
        if choices is None and self.is_enum:
            choices = self.type

        self.choices = choices
        self.help = annotation.get('help')
        self.position = position
        self.takes_value = self.is_positional or (self.is_optional and not self.is_bool)

    def __getattr__(self, name):
        return getattr(self._parameter, name)

    def __str__(self):
        string = '{kind} parameter: {self.name}{default} ({self.type.__name__})'
        kind = 'Positional' if self.is_positional else 'Optional'
        empty = self._parameter.default in (self._parameter.empty, None)
        default = '' if empty else '[={self.default}]'.format_map(locals())
        return string.format_map(locals())


class HelpParameter(Parameter):

    def __init__(self):
        self._parameter = None
        self.real_name = 'help'
        self.name = 'help'
        self.short_option = '-h'
        self.annotation = Arg()
        self.default = False
        self.is_positional = False
        self.is_optional = True
        self.is_keyword_only = False
        self.type = bool
        self.is_bool = True
        self.is_dict = False
        self.is_list = False
        self.is_bool_or = False
        self.choices = None
        self.help = None
        self.position = None
        self.takes_value = False


class bool_or:

    """Used to indicate that an arg can be a flag or an option.

    Use like this::

        @command
        def local(config, cmd, hide: {'type': bool_or(str)} = False):
            "Run the specified command, possibly hiding its output."

    Allows for this::

        run local --hide all     # Hide everything
        run local --hide         # Hide everything with less effort
        run local --hide stdout  # Hide stdout only
        run local --no-hide      # Don't hide anything

    """

    type = None

    def __new__(cls, other_type):
        if not isinstance(other_type, type):
            message = 'Expected type, not {other_type.__class__.__name__}'.format_map(locals())
            raise TypeError(message)
        name = 'BoolOr{name}'.format(name=other_type.__name__.title())
        return type(name, (cls, ), {'type': other_type})


class BoolOrAction(argparse.Action):

    def __call__(self, parser, namespace, value, option_string=None):
        if value is None:
            value = True
        setattr(namespace, self.dest, value)


class DictAddAction(argparse.Action):

    def __call__(self, parser, namespace, item, option_string=None):
        if not hasattr(namespace, self.dest):
            setattr(namespace, self.dest, OrderedDict())

        items = getattr(namespace, self.dest)

        try:
            name, value = item.split('=', 1)
        except ValueError:
            raise CommandError(
                'Bad format for {self.option_strings[0]}; expected: name=<value>; got: {item}'
                .format_map(locals()))

        if value:
            value = JSONValue(value, name=name).load(tolerant=True)
        else:
            value = None

        items[name] = value


class ListAppendAction(argparse.Action):

    def __call__(self, parser, namespace, value, option_string=None):
        if not hasattr(namespace, self.dest):
            setattr(namespace, self.dest, [])
        items = getattr(namespace, self.dest)
        if value:
            name = str(len(items))
            value = JSONValue(value, name=name).load(tolerant=True)
        else:
            value = None
        items.append(value)


# Avoid circular import
from .config import Config, JSONValue, RawConfig, RunConfig  # noqa: E402
