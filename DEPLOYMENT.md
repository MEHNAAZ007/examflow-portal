# Deploying ExamFlow

ExamFlow sends email using one central SMTP account for the entire portal. Teachers never need SMTP credentials, a Gmail account, or access to Brevo. Do not put passwords in `app.py`, commit a `.env` file, or share email app passwords.

## 1. Configure secrets

In your hosting provider's environment-variable settings, add the values shown in [.env.example](.env.example):

- `SECRET_KEY`: a long random value.
- `APP_TIMEZONE`: for example, `Asia/Kolkata`.
- `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`, `MAIL_USE_TLS`. Configure these once using the institution/portal sender email; every Teacher's class notifications will use this sender.
- `TEACHER_INVITE_CODE`: a private code required to create a Teacher account.
- `START_SCHEDULER=true`.

For Gmail, use `smtp.gmail.com`, port `587`, TLS enabled, and a Google **App Password**—not your usual Gmail password. A transactional provider such as Brevo, Postmark, or SendGrid is preferable for production delivery.

## 2. Install and run

```powershell
pip install -r requirements.txt
waitress-serve --host=0.0.0.0 --port=8000 app:app
```

For a cloud host, use its assigned port instead of `8000`. Configure a persistent disk/volume for the `instance/` directory; SQLite data is otherwise lost on many deployments.

## Free Render demo deployment

1. Push this project to a private GitHub repository. The included `render.yaml` creates the service configuration.
2. In Render, choose **New** → **Blueprint**, select the repository, and enter the prompted secret values. Do not enter SMTP credentials in GitHub.
3. The included `render.yaml` uses Render Free and gives you a public `onrender.com` link after deployment.

The free version is for demonstration only: it sleeps after inactivity, its local SQLite database can be erased when it restarts, and it cannot run dependable one-hour email reminders. In-app class notifications still work whenever students open the app. Change to a paid plan plus persistent storage before using it for a real school.

## 3. Reminder worker

The app runs a one-minute scheduler inside the web process. Run **one** application instance when `START_SCHEDULER=true`, otherwise more than one server instance can each attempt reminders. For high-scale deployment, move reminders to a separate job worker and use PostgreSQL instead of SQLite.

## 4. Verify delivery

1. Create a Student account using a real email address.
2. Create a Teacher account using the invite code.
3. Schedule an assessment at least 61 minutes in the future. The Student receives a schedule email immediately.
4. Keep the deployed service running. At the one-hour mark, the Student receives one reminder email.

If an email does not arrive, check the deployment logs for SMTP errors and the recipient's spam folder.
