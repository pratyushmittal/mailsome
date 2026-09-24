Feature: Direct mail ingestion and periodic classification

  Scenario: Load encountered mail without preloading the mailbox
    Given a connected mailbox has a history anchor and an uncached older message
    When history, expired-history recovery, or an on-demand mail view encounters the message
    Then its metadata and available bodies are stored without downloading attachments

  Scenario Outline: Keep the history cursor when synchronization fails
    Given Gmail fails during a sync at "<failure>"
    When history synchronization is attempted
    Then the cursor and sync timestamp do not advance and successful downloads remain

    Examples:
      | failure            |
      | history page       |
      | detail download    |
      | cursor replacement |

  Scenario: Keep message ingestion responsive during classification
    Given the first periodic AI batch pauses while more mail arrives
    Then new mail is saved immediately and classified after the current batch finishes

  Scenario: Resume ingestion after a failed save without skipping history
    Given history and browsing expose the same new message
    When saving the downloaded message fails
    Then the failed save leaves the cursor and sync timestamp unchanged
    When the same history or browsing download is retried
    Then the message is saved and only history synchronization advances the cursor

  Scenario: Classify available stored inbox mail in successive bounded batches
    Given an idle periodic classifier receives more than two batches of stored mail
    When the periodic classifier runs through idle and populated passes
    Then empty passes skip preparation and populated passes drain all bounded batches

  Scenario: Apply sender rules to stored inbox mail independently of AI
    Given an exact sender rule covers older stored mail with exhausted AI attempts
    When the background sync applies sender rules
    Then only exact stored inbox matches receive labels without body downloads or filter reconciliation
    When a matching sender label is manually removed and rules run again
    Then the missing sender label is reapplied
    When the sender rule is removed and rules run again
    Then existing sender labels remain untouched

  Scenario: Save classifications before Gmail writes and retry labels without another paid request
    Given classification succeeds but applying its Gmail labels fails
    When the classifier attempts to save and apply the decisions
    Then each paid email response is saved but no failed Gmail write is acknowledged
    When Gmail recovers and the classifier runs again
    Then saved decisions are applied without downloading the messages again
    When a label is manually removed and its description is edited
    Then completed mail is not reclassified and the manual removal is preserved

  Scenario: Score and reclassify importance without AI-enabled tabs
    Given AI is enabled without tab questions or Gmail label access
    When the worker scores stored mail and I request reclassification
    Then inbox importance is rescored without Gmail writes and archived mail is skipped

  Scenario: Reset selected AI labels before reclassifying the cached inbox
    Given completed mail has selected AI labels, unrelated labels, and stale label snapshots
    When I confirm reclassification of the selected cached inbox mail
    Then selected labels are reset and the messages await the regular classifier
    When the periodic classifier runs after other completed mail arrives
    Then only the confirmed mail is reclassified after protected and unrelated assignments are preserved

  Scenario: Show AI request outcomes and known or unknown costs without private content
    Given a paid classification completes, times out, or returns an incomplete answer
    When the classification request is attempted
    Then Settings shows the request outcome and cost without exposing mail or credentials

  Scenario: Upgrade workflow storage without losing mail or classification history
    Given an older database contains mail, decisions, and usage
    When the database is upgraded to the current schema
    Then mail, preferences, costs, and completion state survive the schema upgrade
