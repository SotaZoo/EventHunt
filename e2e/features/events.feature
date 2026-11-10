Feature: Public preview

  Scenario: Missing JavaScript never puts an email in a URL
    Given the web app is running
    When I open the events page
    Then the invitation form fails safely without JavaScript

  Scenario: Anonymous visitor can preview only the public selection
    Given the web app is running
    When I open the events page
    Then I should see the bounded public preview
    And the landing exposes no restricted controls and its footer is public
    And preview source links return to the same public app

  Scenario: Requesting an invitation preserves a failed submission and then confirms receipt
    Given the web app is running
    When I open the events page
    Then I can retry an invitation request without losing my address

  Scenario: Empty and failed catalogs keep access available
    Given the web app is running
    When I open the events page
    Then empty and failed preview responses keep the request form and sign in

  Scenario: Public preview works across viewport sizes and appearances
    Given the web app is running
    When I open the events page
    Then the public access form fits small mobile and desktop in both appearances

  Scenario: Email entry remains reachable with an obscuring keyboard viewport
    Given the web app is running
    When I open the events page
    Then the invite form remains reachable in a simulated software-keyboard viewport
