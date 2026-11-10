"""Supabase integration boundary."""

from api.integrations.supabase.auth import (  # noqa: F401
    AuthorizationDetails,
    OAuthGrant,
    SupabaseAuthError,
    SupabaseAuthGateway,
    SupabaseIdentity,
    SupabaseOAuthStart,
    SupabaseSession,
)
