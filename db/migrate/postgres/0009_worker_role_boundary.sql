-- Remove auth-data access that the scrape/score worker does not need.
-- Friend rendering keeps only the columns read by compute_friends_going().

REVOKE ALL PRIVILEGES ON TABLE
    public.users,
    public.user_event_states,
    public.linked_identities,
    public.alpha_invites,
    public.scout_metrics
FROM event_scout_worker;

GRANT SELECT (id, display_name, is_active)
    ON TABLE public.users
    TO event_scout_worker;

GRANT SELECT (event_id, user_id, status)
    ON TABLE public.user_event_states
    TO event_scout_worker;

GRANT INSERT ON TABLE public.scout_metrics TO event_scout_worker;

REVOKE SELECT ON SEQUENCE
    public.events_id_seq,
    public.scout_metrics_id_seq
FROM event_scout_worker;

DROP POLICY IF EXISTS linked_identities_worker_read
    ON public.linked_identities;

DROP POLICY IF EXISTS alpha_invites_worker_read
    ON public.alpha_invites;
