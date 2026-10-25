# Mini App progress

## Completed: marketplace banner photo feed

- Marketplace lots now carry short-lived, signed image URLs.
- The server checks the stored banner ownership or active marketplace listing,
  retrieves the image through Telegram, and never sends the bot token to the browser.
- Banner photos and animated level-six GIFs can be shown directly in the feed.
- Existing stored backup references (`file_id` and `market_file_id`) are used.

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

## Next

1. Add banner transfer requests and recipient-side acceptance.
2. Continue migrating other bot tools while keeping Telegram commands available.
