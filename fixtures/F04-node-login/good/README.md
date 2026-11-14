# Club login

The members area for our running club. Sign in to get a token, then use it to see your
profile.

## Run it

```sh
npm install
npm start
```

The server listens on `$PORT` (8080 by default). The demo account is `demo` with the password
`hackathon`.

## API

- `POST /login` with `{"username": "demo", "password": "hackathon"}` returns `{"token": "..."}`,
  or 401 for a wrong username or password.
- `GET /me` with `Authorization: Bearer <token>` returns `{"username": "demo"}`. Without a valid
  token it returns 401.
