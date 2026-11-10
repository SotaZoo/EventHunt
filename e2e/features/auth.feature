Feature: Private access

  Scenario: Anonymous visitor has immediate sign-in access from the preview
    Given the web app is running
    When I open the events page
    Then I should see the bounded public preview
    When I use the profile entry to sign in
    Then I reach the full invited discovery

  Scenario: An uninvited Google account sees recovery immediately
    Given the web app is running
    When I open the events page
    Then wrong-account sign-in opens the request and account-switch controls
