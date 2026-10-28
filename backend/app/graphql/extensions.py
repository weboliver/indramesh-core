"""Shared strawberry SchemaExtensions registered on the GraphQL schema."""

from strawberry.extensions import SchemaExtension


class SoftErrorExtension(SchemaExtension):
    """Appends resolver-collected non-fatal errors to the response's errors[].

    Resolvers that want to report a problem (e.g. Elasticsearch being
    unavailable) without nulling their field's data append a ``GraphQLError``
    to ``info.context.soft_errors`` instead of raising. This extension moves
    those errors onto the execution result after execution completes, so
    ``data`` stays populated (per D-21/D-22 graceful degradation) while the
    client still sees an entry in ``errors[]``.
    """

    def on_execute(self):
        yield
        context = self.execution_context.context
        soft_errors = getattr(context, "soft_errors", None)
        if not soft_errors:
            return
        result = self.execution_context.result
        if result is None:
            return
        result.errors = [*(result.errors or []), *soft_errors]
