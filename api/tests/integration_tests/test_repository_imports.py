def test_repository_modules_are_importable():
    import api.repositories.event_states
    import api.repositories.events
    import api.repositories.identities
    import api.repositories.invites
    import api.repositories.maintenance
    import api.repositories.posting_claims
    import api.repositories.runtime_markers
    import api.repositories.users

    assert api.repositories.events is not None
