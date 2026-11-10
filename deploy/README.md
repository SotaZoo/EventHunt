# Deployment examples

The container builds the discovery assets and serves the application with
the Python CLI. See the [deployment guide](../doc/administration/deployment.md)
and [authentication settings](../doc/administration/authentication.md) for
configuration.

The macOS launchd files are templates for the packaged CLI. Resolve example
paths against your installation and supply your own environment file.
`friends-alpha.env.example` and `env.staging.example` list the expected
settings.
