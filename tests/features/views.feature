Feature: Tabs, search, readers, and explicit mail actions

  Scenario: Create or pin a Gmail label and manage its tab without deleting the label
    Given a Gmail account is connected for tab management
    When I create or pin a label as a tab and open it
    Then the tab selects its label, rejects duplicate pins, and keeps edits
    When I unpin the tab
    Then the tab is gone but its Gmail label is not deleted

  Scenario: Keep tab ordering when adding or editing tabs
    Given two label tabs already have a saved order
    When I reverse their order, append a new tab, and edit an existing tab
    Then the saved order contains the reordered tabs followed by the new tab

  Scenario: Exclude pinned labels and legacy queries from Others
    Given pinned labels and optional legacy query tabs coexist with an unpinned label
    When I open Others
    Then Gmail receives inbox restrictions and exclusions for only the pinned tabs
    When I unpin a label and reopen Others
    Then the removed tab no longer contributes an exclusion

  Scenario: Search all matching Gmail mail regardless of the selected tab
    Given a search query is opened with or without a selected label tab
    Then the first and older pages forward the query without tab or inbox restrictions

  Scenario: Browse a deferred preview of previous mail from the actual sender
    Given sender history includes older mail and a misleading display-name match
    When I open the reader and request its sender-history preview
    Then five other messages from the actual address are shown and the preview is reused

  Scenario: Edit local tab preferences without contacting Gmail
    Given a saved tab exists while Gmail label requests are unavailable
    When I open, validate, edit, and unpin the local tab
    Then the local operations never request Gmail labels

  Scenario: Reuse conversation details until an archive or account change invalidates them
    Given a stored conversation has an older reply
    When I revisit the older reply twice
    Then the conversation is fetched only once
    When archiving fails and I revisit the reader
    Then the failed archive leaves the reader cache usable
    When archiving the conversation succeeds
    Then the archived conversation is fetched again while other conversations and syncs keep the cache
    When the connected account changes
    Then the conversation is fetched again and a disconnected account cannot read it

  Scenario: Reuse reader content after downloading missing fields
    Given a stored message is missing body or action-header data
    When I open that message twice
    Then full details are downloaded only once

  Scenario: Download only the selected attachment
    Given an email has a provider-backed PDF attachment
    When I request the selected attachment
    Then the response contains the bytes from that attachment request

  Scenario: Confirm an unsubscribe before labeling the sender
    Given a connected message advertises an unsubscribe destination
    When I open its unsubscribe page
    Then opening the page makes no label changes
    When I confirm the unsubscribe twice
    Then the same label and sender rule are reused without sending mail

  Scenario: Group sender rules into Gmail filters only when their rules change
    Given two sender addresses share a proposed label beside an unrelated Gmail filter
    When I save the sender-label tab
    Then one additive Gmail filter groups the distinct full addresses
    When I edit only the description and reopen its editor
    Then the description edit does not reconcile Gmail filters
    When I remove one sender from the label
    Then the exact previous filter is replaced while other tab settings remain
    When I unpin the sender-label tab
    Then the unrelated filter and historical Gmail labels remain untouched
