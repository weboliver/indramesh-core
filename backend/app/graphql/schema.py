from functools import partial

import strawberry
from strawberry.extensions import AddValidationRules

from app.graphql.extensions import SoftErrorExtension
from app.graphql.limits import (
    AliasLimitRule,
    CostLimitRule,
    DepthLimitRule,
    LimitsExtension,
)
from app.graphql.resolvers.query import Query

# Pass factories/classes, not instances — strawberry-graphql deprecates
# passing extension instances directly (DeprecationWarning today, will be
# removed in a future release).
# Phase 312 (GQLC-02): the document-level contract rules are added through
# AddValidationRules (rule CLASSES, appended to the execution context's
# validation rules per request) so every execution path built on this list
# inherits them — `/api/v1/graphql`, `/public/api/v1/graphql` (which imports
# `_extensions` below) and the saved-query path, which executes this same
# schema. LimitsExtension arms the parser's token limit and normalises its
# parse abort in place (the abort never reaches a rule).
# The former `partial(QueryDepthLimiter, max_depth=...)` entry is deliberately
# GONE: it reported an extension-less bare GraphQLError and would have
# shadowed the QUERY_TOO_COMPLEX contract for over-depth documents —
# DepthLimitRule now owns the depth axis (same determine_depth semantics, but
# with the contract payload).
# Phase 312 (GQLC-03): CostLimitRule is the fourth axis — it enforces the
# type-aware cost budget with the same contract payload, while the SAME
# accumulator behind it (limits.estimate_cost) feeds the X-GraphQL-Cost
# headers, so header, rejection `actual` and the CLI report one number.
_extensions = [
    SoftErrorExtension,
    partial(AddValidationRules, [AliasLimitRule, DepthLimitRule, CostLimitRule]),
    LimitsExtension,
]
# D-185-01: DisableIntrospection removed. The existing Depends(get_current_user)
# in get_context (context.py) rejects all unauthenticated requests before
# Strawberry processing begins (HTTP 401). Every request that reaches this
# schema IS authenticated, so DisableIntrospection would be dead code that
# only blocks authenticated introspection needed by GraphiQL Docs Explorer
# (Phase 186) and Visual Query Builder (Phase 187).
schema = strawberry.Schema(
    query=Query,
    extensions=_extensions,
)
