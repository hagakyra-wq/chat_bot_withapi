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

## Next

1. Bring the existing fortune-wheel rules into Mini App and add a polished,
   reduced-motion-aware animation.
2. Continue migrating other bot tools while keeping Telegram commands available.
