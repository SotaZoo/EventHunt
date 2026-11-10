Feature: The desktop app keeps the mobile discovery journey
  Scenario: Desktop calendar and mobile calendar share browsing context
    Given I am an invited friend on the mobile home
    Then the desktop calendar preserves selection across month browsing and resizing
    And desktop source and account returns preserve my filters and visible event

  Scenario: Desktop calendar and dialogs remain usable in constrained windows
    Given I am an invited friend on the mobile home
    Then the desktop layout works in both appearances and enlarged short windows

  Scenario: Desktop invitation errors and receipts use the approved layout
    Given the web app is running
    When I open the events page
    Then the desktop invitation journey retains retry and receipt states
