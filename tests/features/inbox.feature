Feature: Gmail connection and classification input

  Scenario: Connect Gmail through local OAuth setup and reload its token
    Given the local Google OAuth setup page is available
    When I upload web credentials and complete Google consent
    Then the account is connected without preloading mail and its token is saved
    When the saved access token expires and Gmail access refreshes it
    Then the renewed token is persisted for subsequent Gmail access

  Scenario: Prepare email state without exposing private content
    Given a message contains markup-like text and private rich content
    When the message state is prepared for classification
    Then the state contains the full text and allowed metadata without changing the stored body
