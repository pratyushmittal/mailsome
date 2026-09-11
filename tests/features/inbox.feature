Feature: A small local Gmail inbox cache
  Mailsome reads recent inbox mail without copying the entire mailbox.

  Scenario: Connect a Gmail account with offline label access
    Given Google can authorize my Gmail account
    When I connect Gmail and complete consent
    Then my account is connected without downloading mail or exposing tokens

  Scenario: Initially fetch only the last two weeks of inbox headers
    Given a connected Gmail account with recent and older mail
    When I refresh the inbox
    Then only recent inbox metadata is cached across all pages
    And changes during the initial download are included

  Scenario: Refresh from the saved history ID
    Given an inbox that has already synchronized
    And Gmail has new mail, an archive, a deletion, and a read-status change
    When I refresh the inbox
    Then those changes are reflected without listing the mailbox again

  Scenario: Rebuild only the recent inbox when history expires
    Given an inbox that has already synchronized
    And Gmail no longer recognizes the saved history ID
    When I refresh the inbox
    Then the cache is rebuilt using only the recent inbox

  Scenario: Retry a failed refresh without skipping any changes
    Given an inbox that has already synchronized
    And fetching a changed message fails after another message was updated
    When I refresh the inbox
    Then the old cache and history ID are preserved
    When Gmail recovers and I refresh again
    Then all changes are applied from the original history ID

  Scenario: Fetch a body only when its message is opened
    Given an inbox that has already synchronized
    When I open the same message twice
    Then its body is downloaded once and cached without fetching attachments

  Scenario: Prune old messages even when Gmail has no new events
    Given an inbox that has already synchronized
    And a cached message has aged beyond two weeks
    When I refresh the inbox
    Then that message is removed only from the local cache

  Scenario: Keep a message body across an expired-history rebuild
    Given an inbox that has already synchronized
    And I have already opened a cached message
    And Gmail no longer recognizes the saved history ID
    When I refresh the inbox
    Then the retained message body is still cached

  Scenario: Set up Google OAuth from the Connect Gmail page
    Given Google OAuth credentials have not been configured
    When I visit Connect Gmail
    Then I see Google Cloud setup instructions and a credentials file picker
    When I upload my downloaded Google web client JSON
    Then Mailsome saves it privately and can start Google sign-in

  Scenario: See real progress while the initial inbox is downloading
    Given a connected Gmail account with recent and older mail
    When Gmail pauses partway through downloading headers
    Then I can see completed header counts while the refresh is still running
    When Gmail finishes responding
    Then progress reports completion only after the cache and cursor are committed

  Scenario: Pin an existing Gmail label as a tab
    Given an inbox that has already synchronized
    When I add an existing Gmail label as a tab
    Then the tab filters cached messages by Gmail label without a network search

  Scenario: Create a new Gmail label from Add
    Given an inbox that has already synchronized
    When I add a label that does not exist in Gmail
    Then Gmail creates the label and Mailsome pins it

  Scenario: Reject an already pinned Gmail label
    Given an inbox that has already synchronized
    When I add an existing Gmail label as a tab
    And I add the same label again
    Then I see that the label is already added

  Scenario: Apply sender labels without AI or downloading bodies
    Given an inbox that has already synchronized
    And a label has an exact sender rule
    When background labeling runs
    Then recent matching messages get the label without a body download

  Scenario: Classify recent mail in a structured batch
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    When background labeling classifies a batch
    Then classifications and reasons are saved before additive Gmail writes

  Scenario: Browse previous conversations from the same sender
    Given an inbox that has already synchronized
    And Gmail has older archived conversations from this sender
    When I request the sender's previous conversations
    Then only one page of conversation headers is downloaded
    When I open an older conversation
    Then its message body loads without expanding the inbox cache


  Scenario: Others contains mail outside all pinned label tabs
    Given an inbox that has already synchronized
    And recent messages belong to two label tabs or an unpinned Gmail label
    When I open Others
    Then only mail outside both tabs is shown without a Gmail search

  Scenario: Others also excludes matches from legacy query tabs
    Given an inbox that has already synchronized
    And recent messages belong to two label tabs or an unpinned Gmail label
    And a legacy query tab matches the remaining recent message
    When I open Others
    Then Others is empty and Gmail evaluated the legacy query without downloading mail

  Scenario: Save reordered tabs without changing classification policy
    Given an inbox that has already synchronized
    And recent messages belong to two label tabs or an unpinned Gmail label
    When I move the last label tab to the first label position
    Then the tab order survives reinitialization without changing labels or AI policy

  Scenario: Explicit search finds categorized mail from Others
    Given an inbox that has already synchronized
    And recent messages belong to two label tabs or an unpinned Gmail label
    When I search the recent inbox from Others
    Then search includes categorized and uncategorized matches but not older mail


  Scenario: Show total AI cost and request history in Settings
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And OpenAI returns measured usage for the classification batch
    When background labeling runs
    Then Settings shows the estimated USD total and a content-free request log

  Scenario: Gmail retries do not count as new AI spending
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And OpenAI returns measured usage for the classification batch
    When applying the saved AI labels to Gmail fails
    And I retry labeling after Gmail recovers
    Then the usage history contains only one paid AI request

  Scenario: Failed AI requests have unknown cost rather than zero
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And the AI request fails without reporting usage
    When background labeling fails
    Then Settings shows a failed request with unknown cost and no private diagnostics

  Scenario: Archive an opened email without deleting it or marking it read
    Given an inbox that has already synchronized
    When I archive the opened email
    Then it leaves the inbox cache but remains unread in Gmail

  Scenario: Keep an editable note for a sender independently of cached mail
    Given an inbox that has already synchronized
    When I add and edit a sender note
    Then the latest note survives inbox cache pruning

  Scenario: Select automatic labels for a sender from the reader
    Given an inbox that has already synchronized
    When I select a label to always apply to the sender
    Then sender rules apply without changing AI settings or removing existing labels

  Scenario: Confirm an unsubscribe before labeling the sender
    Given an inbox that has already synchronized
    When I open an email with an unsubscribe option
    Then no unsubscribe or Gmail write happens merely by opening the email
    When I confirm that I have unsubscribed
    Then the unsubscribed label and sender rule are saved
