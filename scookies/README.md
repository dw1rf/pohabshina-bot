# YouTube cookies

Put the real `youtube-cookies.txt` file in this folder on the server.

The real cookie file must not be committed to Git. It contains an authenticated YouTube session.

Expected runtime path:

```env
YTDLP_COOKIE_FILE=scookies/youtube-cookies.txt
```

The file must be exported in Netscape cookies format.
