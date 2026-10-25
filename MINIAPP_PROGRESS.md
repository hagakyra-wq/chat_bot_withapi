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

## Next

1. Add the user's banner inventory as a carousel with safe image previews.
2. Bring the existing fortune-wheel rules into Mini App and add a polished,
   reduced-motion-aware animation.
3. Continue migrating other bot tools while keeping Telegram commands available.
