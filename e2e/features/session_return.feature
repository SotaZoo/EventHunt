Feature: Access rechecked after leaving discovery

  Scenario: Revocation removes private results on source return and in another open tab
    Given I am an invited friend on the mobile home
    Then revoked access cannot retain the private catalog across Back and other tabs

  Scenario: Sign out removes private results from another open tab
    Given I am an invited friend on the mobile home
    Then signing out also clears private results in another tab
