# Mini App progress

## Completed: marketplace banner photo feed

- Marketplace lots now carry short-lived, signed image URLs.
- The server checks the stored banner ownership or active marketplace listing,
  retrieves the image through Telegram, and never sends the bot token to the browser.
- Banner photos and animated level-six GIFs can be shown directly in the feed.
- Existing stored backup references (`file_id` and `market_file_id`) are used.
- Missing previews are now recovered from their backup or rendered on the first
  image request, including inventory banners and marketplace-specific artwork.

Telegram Bot API does not provide a method to enumerate historical channel
messages. This section therefore displays images attached to banner records
already indexed by the bot; it does not import unindexed photos from the
channel. A historical backfill requires an authorized Telegram user session
with access to the backup channel.

## Completed: inventory carousel

- The profile shows the user's stored banners in a responsive carousel with image
  previews, rarity, active state, and previous/next controls.
- Users can activate an owned banner from the carousel; the operation is checked
  and performed server-side, including protection against activating a listed lot.
- Existing Telegram inventory and activation commands remain available.

## Completed: fortune wheel

- Added single and ×10 spins to the Mini App, using the existing cost, rarity
  rules, server-side random rolls, and casino-round debit/refund transactions.
- The wheel sectors and displayed odds use the same shared rarity weights as the
  Telegram bot. Results are stored as banner inventory items before animation.
- Added a multi-turn wheel animation, result cards, balance display, and
  reduced-motion support. Reward artwork is prepared in the background and
  preparation failures are logged without losing the saved prize.

## Completed: inventory recycling

- Added an irreversible recycle action with a confirmation prompt and the
  configured coin payout shown beside each eligible banner.
- The server derives the payout from banner rarity and executes the existing
  transactional recycle operation; active, owner, listed, pending, and
  unowned banners cannot be recycled.
- Backup-channel copies are removed after successful recycling when the bot
  has channel access.

## Completed: banner transfers

- Added a profile transfer panel to send a request by Telegram ID or username,
  review incoming and outgoing requests, accept or return incoming banners, and
  cancel outgoing requests.
- Existing ownership, marketplace, and recipient-consent checks remain enforced
  by the database transactions; bot notifications and chat actions still work.
- Signed image access for a pending transfer is limited to its sender,
  recipient, or a public marketplace listing.

## Completed: community utilities

- Added group message and coin leaderboards for groups the user is recorded as
  currently belonging to and the bot is allowed to use.
- Added Mini App error reports delivered to the bot owner.
- Added an explicit, confirmed action to delete profile data and message
  activity from all groups while preserving balance and banner inventory.
- Added an image prompt form that hands the request to the existing bot
  generation workflow in the user's private chat.
- Added profile editing for callsign, age, gender, and appending a profile fact.
  Profile edits use the existing shared profile data and apply across groups.

## Chat-only operations

- Group message moderation and reactions need a target chat/message context.
- Duels require a second participant and their consent.
- Auto-responder controls, personal Telegram account authorization, group
  management, and history exports remain in their existing bot conversations.
- Owner-only service shutdown and full data reset, plus personal-account
  credentials and banner-pattern administration, are intentionally not exposed
  in the Mini App.
- Image prompts can be submitted in Mini App, but generation configuration and
  final delivery still take place in the private bot chat.
- Deleting profile data does not remove archived message contents; it removes
  profile fields, group user records, and message activity counters.

These operations remain available in Telegram; Mini App does not claim to replace
the Telegram chat context or the personal-account authorization flow.
