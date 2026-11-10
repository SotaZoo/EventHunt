Feature: Invited friends discover events
  Scenario: A reviewed time conflict stays visible with an accessible warning
    Given I am an invited friend on the mobile home
    Then a reviewed time conflict is visible alongside its waitlist status

  Scenario: Urgency expires in an open agenda without changing browsing context
    Given I am an invited friend on the mobile home
    Then expired urgency disappears without a reload or date change

  Scenario: Scan concrete highlights with quiet excellent-fit emphasis
    Given I am an invited friend on the mobile home
    Then event facts follow the approved card order
    And only scores at or above 0.90 receive quiet emphasis
    And long highlights wrap naturally at enlarged mobile text sizes

  Scenario: Browse only unique eligible occurrences and identify a waitlist
    Given I am an invited friend on the mobile home
    Then duplicate and terminal listings do not consume discovery results
    And the waitlist card remains available with its source link

  Scenario: Browse the approved mobile discovery home
    Given I am an invited friend on the mobile home
    Then I can scan two events on the same day
    When I expand the month and choose another day
    Then the calendar remains expanded
    When I return to Today
    Then I can scan two events on the same day
    When I open an event source and return
    Then my calendar and browsing context are preserved

  Scenario: Preview, cancel, apply and reset filters without changing dates
    Given I am an invited friend on the mobile home
    Then I can preview and cancel a format filter
    And I can combine score and start-time filters
    And I can reset only the draft or apply the reset

  Scenario: Use calendar gestures, month navigation and agenda scrolling
    Given I am an invited friend on the mobile home
    Then I can drag the calendar open and closed with touch
    Then I can browse weeks continuously and choose a leap date
    And scrolling the agenda follows the day without folding

  Scenario: Accessible appearances and account access
    Given I am an invited friend on the mobile home
    Then light and dark appearances use the same readable text roles
    And the expanded calendar works on a short enlarged viewport
    And I can reach account and agent settings

  Scenario: Recover from a catalog failure
    Given the discovery catalog is temporarily unavailable
    Then I can retry loading events

  Scenario: Keep refresh failure visible until recovery
    Given I am an invited friend on the mobile home
    Then a failed refresh cannot be dismissed by calendar navigation
    And I can retry loading events

  Scenario: Source and model content never becomes executable markup
    Given I am an invited friend on the mobile home
    Then source content is displayed only as text
