from api.repositories.migrate import _iter_statements, get_migration_dirs


def test_migration_dirs_are_under_db_migrate(repo_root):
    dirs = get_migration_dirs(repo_root)
    assert dirs["sqlite"].relative_to(repo_root).as_posix() == "db/migrate/sqlite"
    assert dirs["postgres"].relative_to(repo_root).as_posix() == "db/migrate/postgres"


def test_supabase_postgres_migration_denies_data_api_and_scopes_oauth_audience(repo_root):
    migration = (
        repo_root / "db" / "migrate" / "postgres" / "0007_supabase_auth.sql"
    ).read_text()

    assert "CREATE TABLE IF NOT EXISTS app_config" in migration
    assert "event_scout_custom_access_token_hook" in migration
    assert "claims'->>'client_id'" in migration
    assert "'{aud}'" in migration
    assert "mcp_resource_url" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "REVOKE ALL" in migration
    assert "FROM anon, authenticated" in migration
    assert "TO supabase_auth_admin" in migration

    statements = list(_iter_statements(migration))
    hook_statements = [
        statement
        for statement in statements
        if "event_scout_custom_access_token_hook" in statement
        and statement.lstrip().upper().startswith("CREATE")
    ]
    assert len(hook_statements) == 1


def test_postgres_worker_role_is_created_and_rls_scoped(repo_root):
    migration = (
        repo_root / "db" / "migrate" / "postgres" / "0008_worker_role.sql"
    ).read_text()

    assert "CREATE ROLE event_scout_worker NOLOGIN" in migration
    assert "GRANT USAGE ON SCHEMA public TO event_scout_worker" in migration
    assert "GRANT SELECT" in migration
    assert "GRANT INSERT, UPDATE" in migration
    assert "GRANT DELETE ON TABLE public.runtime_markers, public.cycle_locks" in migration
    assert "GRANT INSERT ON TABLE public.scout_metrics" in migration
    assert "GRANT USAGE, SELECT ON SEQUENCE public.events_id_seq" in migration
    assert "TO event_scout_worker" in migration
    assert "CREATE POLICY events_worker_all" in migration
    assert "CREATE POLICY users_worker_read" in migration
    assert "CREATE POLICY schema_migrations_worker_read" in migration
    assert "GRANT ALL" not in migration
    assert "ON ALL TABLES" not in migration
    assert "BYPASSRLS" not in migration

    statements = list(_iter_statements(migration))
    assert statements[0].strip() == "CREATE ROLE event_scout_worker NOLOGIN"


def test_postgres_worker_role_denies_auth_data_and_scopes_friend_signal(repo_root):
    migration = (
        repo_root / "db" / "migrate" / "postgres" / "0009_worker_role_boundary.sql"
    ).read_text()

    assert "REVOKE ALL PRIVILEGES ON TABLE" in migration
    assert "public.users" in migration
    assert "public.user_event_states" in migration
    assert "public.linked_identities" in migration
    assert "public.alpha_invites" in migration
    assert "public.scout_metrics" in migration
    assert "GRANT SELECT (id, display_name, is_active)" in migration
    assert "GRANT SELECT (event_id, user_id, status)" in migration
    assert "GRANT INSERT ON TABLE public.scout_metrics" in migration
    assert "DROP POLICY IF EXISTS linked_identities_worker_read" in migration
    assert "DROP POLICY IF EXISTS alpha_invites_worker_read" in migration
    assert "GRANT SELECT ON TABLE public.users" not in migration
    assert "GRANT SELECT ON TABLE public.user_event_states" not in migration
    assert "GRANT SELECT ON TABLE public.linked_identities" not in migration
    assert "GRANT SELECT ON TABLE public.alpha_invites" not in migration
