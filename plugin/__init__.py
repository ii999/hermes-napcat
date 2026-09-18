"""Directory-plugin entry point. The implementation lives in the installed package."""

def register(ctx):
    from hermes_napcat.plugin import register as register_platform
    register_platform(ctx)
