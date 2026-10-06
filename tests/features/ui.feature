Feature: Reading mail and using the local interface

  Scenario: Read a conversation and select a reply with its original recipients
    Given a conversation contains older mail, sent replies, and original recipient headers
    When I open the conversation from Others
    Then the conversation and original recipients are displayed safely
    When I select the sent reply
    Then the reply is selected without inventing missing recipients or loading its HTML attachment
    When I request the selected reply formatted body
    Then only its HTML body attachment is downloaded and displayed

  Scenario: Read formatted email without running sender content or loading trackers
    Given an email contains formatted HTML, an embedded image, and a tracker
    When I open the reader and its formatted body
    Then formatted content and the embedded image appear while scripts and trackers are blocked
    When I explicitly allow external images for one view
    Then that view allows the remote image
    When I open the formatted body again without image consent
    Then external images are blocked again

  Scenario: Edit sender notes with plain forms independently of cached mail
    Given a stored sender has a note editor in the reader
    When I create and edit a sender note using its plain form
    Then the note survives removal of cached mail and is cleared without Gmail writes

  Scenario: Navigate the mail list and return from the reader with the keyboard
    Given a keyboard-driven mailbox
    Then mail navigation restores selection after opening and returning, preloads and opens the next mail after archiving, and respects list boundaries

  Scenario: Use reader action shortcuts without conflicting with editing or sender history
    Given a keyboard-driven mailbox
    Then reader shortcuts map to their actions, ignore editors, and distinguish reply from sender history
