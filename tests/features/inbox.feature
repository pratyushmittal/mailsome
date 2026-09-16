Feature: A small local Gmail inbox cache
  Mailsome caches encountered mail without copying the entire mailbox.

  Scenario: Connect a Gmail account with offline label access
    Given Google can authorize my Gmail account
    When I connect Gmail and complete consent
    Then my account is connected without downloading mail or exposing tokens

  Scenario: Load existing inbox mail only when its list is opened
    Given a connected Gmail account with recent and older mail
    When I refresh the inbox
    Then inbox metadata and available bodies are cached together
    And the cursor is captured before the first page is loaded

  Scenario: Refresh from the saved history ID
    Given an inbox that has already synchronized
    And Gmail has new mail, an archive, a deletion, and a read-status change
    When I refresh the inbox
    Then those changes are reflected without preloading inbox mail

  Scenario: Replace expired history without rebuilding or deleting cached mail
    Given an inbox that has already synchronized
    And Gmail no longer recognizes the saved history ID
    When I refresh the inbox
    Then a new cursor is saved without rebuilding cached content

  Scenario: Retry a failed refresh without skipping any changes
    Given an inbox that has already synchronized
    And fetching a changed message fails after another message was updated
    When I refresh the inbox
    Then completed downloads are saved without advancing the history ID
    When Gmail recovers and I refresh again
    Then all changes are applied from the original history ID

  Scenario: Reuse a body fetched during sync when its message is opened
    Given an inbox that has already synchronized
    When I open the same message twice
    Then its cached body is reused without another detail or attachment request

  Scenario: Retain old messages when Gmail has no new events
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

  Scenario: See real progress while history messages are downloading
    Given a connected Gmail account with recent and older mail
    When Gmail pauses partway through downloading headers
    Then I can see completed header counts while the refresh is still running
    When Gmail finishes responding
    Then progress reports completion only after the cache and cursor are committed

  Scenario: Pin an existing Gmail label as a tab
    Given an inbox that has already synchronized
    When I add an existing Gmail label as a tab
    Then Gmail selects the tab while cached metadata is reused

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
    Then only one page of message details is downloaded and cached
    When I open an older conversation
    Then its message body is retained without expanding AI eligibility


  Scenario: Others contains mail outside all pinned label tabs
    Given an inbox that has already synchronized
    And recent messages belong to two label tabs or an unpinned Gmail label
    When I open Others
    Then Gmail selects only mail outside both pinned tabs

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
    Then search includes all Gmail matches regardless of cache age


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
    Then it leaves the inbox view but stays cached and unread in Gmail

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

  Scenario: Navigate the mail list and return from the reader with the keyboard
    Given a displayed list of three emails
    When I press j twice and k once to select an email
    And I press Enter to open the selected email
    And I press Escape to return to the mail list
    Then the same email is highlighted and focused without changing the list

  Scenario: Edit a sender note with a plain server-rendered form
    Given an inbox that has already synchronized
    When I submit the sender note form without JavaScript
    Then the server redirects to the reader and safely renders my saved note

  Scenario: Reject a forged plain form submission
    Given an inbox that has already synchronized
    When I submit a sender note without its CSRF token
    Then the server rejects it without changing the sender note

  Scenario: See the original recipients of a forwarded email
    Given an inbox that has already synchronized
    And a message was addressed to other recipients and delivered to my account
    When I open that message to inspect its recipients
    Then the reader shows its To, Cc, and Delivered-To headers without inventing Bcc

  Scenario: Production scheduled sync remains responsive during paid classification
    Given the workflow loops have a connected inbox and an enabled AI label
    When scheduled and manual sync overlap a real paid classification
    Then sync finishes without blocking forms or archive and only one AI attempt is billed

  Scenario: Remove the old queue without transferring requests or losing AI usage
    Given old workflow state and saved AI usage
    When I replace the old queue with workflow loops
    Then old requests are discarded while usage and explicit paid-retry safeguards remain

  Scenario: Read an email with its earlier messages and later replies
    Given an inbox message belongs to a conversation with older, sent, and later replies
    When I open that message in its conversation
    Then all conversation headers are shown and fetched bodies are cached for reuse
    When I select the sent reply
    Then its sender and recipients are selected without expanding AI eligibility

  Scenario: Read formatted email without running sender content or loading trackers
    Given an inbox message has a formatted HTML body with an embedded image and a tracker
    When I open its formatted body
    Then formatting and the embedded image appear but active content and external images are blocked
    When I explicitly allow external images
    Then only that view permits HTTPS images and the plain text option remains available

  Scenario: Archive the whole conversation without changing read status
    Given an inbox message belongs to a conversation with older, sent, and later replies
    When I archive the opened email
    Then the entire conversation is archived without marking any message read

  Scenario: Download an attachment only when requested
    Given an inbox message has a formatted HTML body with an embedded image and a tracker
    When I open the attachment list and download the invoice
    Then only the selected attachment is downloaded and no mail state changes

  Scenario: Reuse recently read replies without blocking on sender history
    Given an inbox message belongs to a conversation with older, sent, and later replies
    When I revisit the inbox message and an older reply
    Then bodies and conversation headers are reused without loading sender history

  Scenario: See attachments before opening a message
    Given an inbox and a conversation contain messages with file attachments and inline logos
    When I view the inbox and the collapsed conversation headers
    Then attachment counts are visible without separate attachment downloads

  Scenario: Hand off a reply to Gmail without sending from Mailsome
    Given an inbox message belongs to a conversation with older, sent, and later replies
    When I open the older reply to respond in Gmail
    Then the Gmail action targets the selected message and connected account without creating or sending mail
    And r opens the Gmail action without interfering with grr or typing

  Scenario: Edit a pinned label while mailbox sync is busy
    Given an existing pinned label tab
    When I open and save its local settings while sync holds the mailbox lock
    Then the editor and save finish without waiting for sync or calling Gmail

  Scenario: Correct an invalid AI batch before applying labels
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And OpenAI omits the messages once before correcting its answer
    When background labeling runs
    Then only the corrected decisions are saved and both requests are counted

  Scenario: Classify typed emails as XML and accept no matching labels
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And recent emails contain XML-like text, recipients and attachment metadata
    When background labeling runs
    Then the classifier receives escaped XML without changing stored email bodies
    And a null label list is saved as classified without a Gmail label write

  Scenario: Apply sender rules while AI labeling is paused
    Given an inbox that has already synchronized
    And a label has an exact sender rule
    When the sync worker runs while AI labeling requires an explicit retry
    Then sender labels are applied without downloading bodies or retrying AI

  Scenario: Retain completed classifications after editing a description
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And OpenAI returns measured usage for the classification batch
    When I classify the inbox and edit its label description
    Then already classified messages are not sent to AI again

  Scenario: Reset selected AI labels before reclassifying the cached inbox
    Given an inbox that has already synchronized
    And I enabled AI for a described label
    And OpenAI returns measured usage for the classification batch
    When I confirm reclassification after a completed batch
    Then selected classification labels are replaced while other mail and usage history remain intact

  Scenario: Browse archived Gmail results without classifying archived mail
    Given an inbox that has already synchronized
    When Gmail returns an older message on two search pages
    Then its metadata and body are reused without enabling AI or advancing history

  Scenario: Classify all cached inbox mail without a fixed window
    Given an inbox with 1005 messages and older unprocessed mail
    When cached inbox mail is classified after cursor sync
    Then all unprocessed cached inbox mail is evaluated without preloading
    When a new message arrives in the full inbox
    Then the new arrival is evaluated without repeating completed mail

  Scenario: Apply sender labels to cached inbox mail only
    Given an inbox that has already synchronized
    When I add a sender rule with older archived matches
    Then only cached inbox matches receive bulk labels without reading bodies
    When I remove the sender rule
    Then existing sender labels remain untouched

  Scenario Outline: Open cached lists and settings while a mailbox request is running
    Given an inbox that has already synchronized
    When I open "<path>" while background sync holds the mailbox lock
    Then the page finishes before background sync releases the mailbox lock

    Examples:
      | path               |
      | /settings/         |
      | /                  |

  Scenario: Resume an interrupted history download without fetching saved headers again
    Given an history download fails after fetching one message
    When the history download is retried
    Then the saved message headers are reused and history is committed only on success

  Scenario: Group sender rules into Gmail filters on rule edits
    Given two senders assigned to one label and an unrelated Gmail filter
    When the sender filter edit is saved
    Then one additive Gmail filter groups both senders and the unrelated filter is untouched
    When one sender is removed from the label
    Then the managed filter is replaced without removing historical labels

  Scenario: Show progress while a sender bulk write is running
    Given an inbox that has already synchronized
    When Gmail pauses during a sender bulk write
    Then progress shows the pending sender batch without exposing message content
