import json
import re


def camel_to_underscore(name):
    """Convert camel case name to underscore name.

    Examples::

        >>> camel_to_underscore('HttpRequest')
        'http_request'
        >>> camel_to_underscore('httpRequest')
        'http_request'
        >>> camel_to_underscore('HTTPRequest')
        'http_request'
        >>> camel_to_underscore('myHTTPRequest')
        'my_http_request'
        >>> camel_to_underscore('MyHTTPRequest')
        'my_http_request'
        >>> camel_to_underscore('my_http_request')
        'my_http_request'
        >>> camel_to_underscore('MyHTTPRequestXYZ')
        'my_http_request_xyz'
        >>> camel_to_underscore('_HTTPRequest')
        '_http_request'
        >>> camel_to_underscore('Request')
        'request'
        >>> camel_to_underscore('REQUEST')
        'request'
        >>> camel_to_underscore('_Request')
        '_request'
        >>> camel_to_underscore('__Request')
        '__request'
        >>> camel_to_underscore('_request')
        '_request'
        >>> camel_to_underscore('Request_')
        'request_'

    """
    name = re.sub(r'(?<!\b)(?<!_)([A-Z][a-z])', r'_\1', name)
    name = re.sub(r'(?<!\b)(?<!_)([a-z])([A-Z])', r'\1_\2', name)
    name = name.lower()
    return name


def load_json_value(string, tolerant=True):
    """Load JSON-encoded string.

    Args:
        string: A JSON-encoded string.
        tolerant: If set, return the string as is if it can't be loaded
            as JSON.

    Returns:
        object: String decoded from JSON

    """
    if not string:
        return None
    try:
        value = json.loads(string)
    except ValueError:
        if not tolerant:
            raise
        value = string
    return value


def load_json_item(string, tolerant=True):
    """Load 'key=value' string where the value is JSON-encoded.

    Args:
        string: A 'key=value' string where the value is JSON-encoded
        tolerant: If set, return the string as is if it can't be loaded
            as JSON.

    Returns:
        (name, object): Name and value string decoded from JSON

    """
    try:
        name, value = string.split('=', 1)
    except ValueError:
        message = 'Bad format for JSON item; expected: name=<value>; got: {string}'
        message = message.format_map(locals())
        raise ValueError(message) from None
    value = load_json_value(value, tolerant)
    return name, value
