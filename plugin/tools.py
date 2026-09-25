"""Deferred Hermes tool entry point; the implementation is in the installed package."""


def register_tools(ctx):
    from hermes_napcat.group_tools import register_tools as register_package_tools

    register_package_tools(ctx)
