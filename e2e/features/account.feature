Feature: Account and AI integration
  Scenario: Copy MCP setup and return to browsing
    Given I am an invited friend on the mobile home
    When I open AI integration through Account
    Then I can copy the complete generic MCP setup prompt
    And I can return through Account to Events

  Scenario: Readable shared design on mobile, dark and enlarged screens
    Given I am an invited friend on the mobile home
    When I open AI integration through Account
    Then the account flow is readable in both appearances and enlarged text

  Scenario: Readable removal confirmation with long client metadata
    Given I am an invited friend on the mobile home
    When I open AI integration through Account
    Then removal confirmation remains readable with a long client name

  Scenario: Recover from clipboard failure
    Given I am an invited friend on the mobile home
    When I open AI integration through Account
    Then failed copying leaves a visible manual-copy instruction

  Scenario: Confirm removal and preserve cancellation
    Given I am an invited friend on the mobile home
    When I open AI integration through Account
    Then I can cancel removal and keep the client connected
    And confirming removal shows the empty authorized-client state

  Scenario: Allow or cancel returns to the requesting AI app
    Given I am an invited friend on the mobile home
    Then allowing access returns to the AI app
    And cancelling access returns to the AI app

  Scenario: Cancelled Google sign-in retains the pending authorization
    Given I am an invited friend on the mobile home
    Then I can retry cancelled sign-in and review consent before returning to my AI app

  Scenario: Expired website session requires a fresh consent decision
    Given I am an invited friend on the mobile home
    Then an expired website session signs in again without automatically approving access

  Scenario: Provider and expired-request failures remain recoverable
    Given I am an invited friend on the mobile home
    Then unavailable access information is not shown as an empty client list
    And failed removal keeps the client available for retry
    And an expired authorization explains how to restart Connect

  Scenario: Sign out returns to the public preview and invitation form
    Given I am an invited friend on the mobile home
    Then I can read About and sign out from Account
