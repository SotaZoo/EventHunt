-- Least-privilege database role used by the local scrape/score worker.
-- Login principals and passwords are created operationally and inherit this
-- non-login role; credentials never belong in migrations or Git.

CREATE ROLE event_scout_worker NOLOGIN;

GRANT USAGE ON SCHEMA public TO event_scout_worker;

GRANT SELECT ON TABLE
    public.events,
    public.users,
    public.user_event_states,
    public.linked_identities,
    public.alpha_invites,
    public.runtime_markers,
    public.cycle_health,
    public.cycle_locks,
    public.scout_metrics,
    public.schema_migrations
TO event_scout_worker;

GRANT INSERT, UPDATE ON TABLE
    public.events,
    public.runtime_markers,
    public.cycle_health,
    public.cycle_locks
TO event_scout_worker;

GRANT DELETE ON TABLE public.runtime_markers, public.cycle_locks
TO event_scout_worker;

GRANT INSERT ON TABLE public.scout_metrics TO event_scout_worker;

GRANT USAGE, SELECT ON SEQUENCE public.events_id_seq TO event_scout_worker;
GRANT USAGE, SELECT ON SEQUENCE public.scout_metrics_id_seq TO event_scout_worker;

CREATE POLICY events_worker_all
    ON public.events
    FOR ALL
    TO event_scout_worker
    USING (TRUE)
    WITH CHECK (TRUE);

CREATE POLICY users_worker_read
    ON public.users
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);

CREATE POLICY user_event_states_worker_read
    ON public.user_event_states
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);

CREATE POLICY linked_identities_worker_read
    ON public.linked_identities
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);

CREATE POLICY alpha_invites_worker_read
    ON public.alpha_invites
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);

CREATE POLICY runtime_markers_worker_all
    ON public.runtime_markers
    FOR ALL
    TO event_scout_worker
    USING (TRUE)
    WITH CHECK (TRUE);

CREATE POLICY cycle_health_worker_all
    ON public.cycle_health
    FOR ALL
    TO event_scout_worker
    USING (TRUE)
    WITH CHECK (TRUE);

CREATE POLICY cycle_locks_worker_all
    ON public.cycle_locks
    FOR ALL
    TO event_scout_worker
    USING (TRUE)
    WITH CHECK (TRUE);

CREATE POLICY scout_metrics_worker_read
    ON public.scout_metrics
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);

CREATE POLICY scout_metrics_worker_insert
    ON public.scout_metrics
    FOR INSERT
    TO event_scout_worker
    WITH CHECK (TRUE);

CREATE POLICY schema_migrations_worker_read
    ON public.schema_migrations
    FOR SELECT
    TO event_scout_worker
    USING (TRUE);
