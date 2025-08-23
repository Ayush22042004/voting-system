# Deploy to Fly.io with Persistent SQLite

This repo includes a `Dockerfile` and `fly.toml` so you can deploy your Flask app with a persistent volume for SQLite.

## Prereqs
- Install Fly CLI: https://fly.io/docs/hands-on/install-flyctl/
- Sign up and log in: `fly auth signup` then `fly auth login`

## Steps
1. In the project directory:
   ```bash
   fly launch --no-deploy
   ```
   - When asked about a database, choose **No** (we use SQLite with a volume).
   - This will set your app name in `fly.toml`.
2. Create a persistent volume (1GB is enough to start):
   ```bash
   fly volumes create data --size 1
   ```
3. Deploy:
   ```bash
   fly deploy
   ```
4. View logs:
   ```bash
   fly logs
   ```
5. Open the app:
   ```bash
   fly open
   ```

## Notes
- The SQLite database file will live at `/data/voting.db` on the attached volume.
- On first boot, the app will auto-initialize the DB if the file is missing.
- If you later switch to Postgres, set `DATABASE_URL` and refactor DB calls accordingly.
