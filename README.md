# ESPN Fantasy Dashboard

A live dashboard for ESPN fantasy football. Create an account, paste the link to your team on ESPN, and see
every league you play in on one page. Python 3.9+, standard library only, no build step.

**Hosted version:** https://espn-fantasy-football-dashboard.up.railway.app

## What it shows

**Matchup.** Live score, ESPN's projected final and win probability, and which starters on each side are
finished, playing or yet to play. Refreshes every minute while games are on.

**Needs attention.** Starters who are out, doubtful, questionable or on bye (with the best healthy backup),
start/sit swaps that raise your projection, waiver pickups that beat your weakest starter, and next week's byes.

**Lineup.** Every player's game state, score and clock, a red-zone flag, and the Vegas implied team total for
upcoming games.

**Season.** Weekly scores against the league median, all-play record, schedule luck, lineup efficiency (points
scored as a share of your best possible lineup), the playoff race and remaining strength of schedule.

**Knockout leagues.** The lowest score among teams still alive goes out each week. Instead of a matchup you see
your score against the cut line, ESPN's survival odds, a board of every live team, who has been eliminated, and
your closest calls.

**Waivers.** The most-rostered available players at your starting positions, ranked by how much each beats your
weakest starter on ESPN's rest-of-season projection, with Sleeper's trending adds.

## Using the hosted version

1. Create an account. It's a username and password; there's no email and no password reset.
2. Open your team on fantasy.espn.com and copy the address, e.g.
   `https://fantasy.espn.com/football/team?leagueId=123456&teamId=4&seasonId=2026`.
   A link with only a `leagueId` works too; you'll pick your team from a list.
3. Paste it in. You can follow up to 12 leagues.

**Private leagues** also need two ESPN cookies, `espn_s2` and `SWID`. In Chrome, open fantasy.espn.com, then
DevTools, Application, Cookies, `https://fantasy.espn.com`. The cookies are kept in your browser only and sent with
each dashboard request; the server uses them to call ESPN and never saves them. They work like a sign-in to your
ESPN account, so only paste them on devices you trust, and remove them by signing out.

## Run your own

### Multi-user web app (what the hosted version runs)

    python3 webapp.py                      # http://localhost:8765, data in ./data

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 8765 | Port. When set, the server listens on all interfaces and marks cookies `Secure`. |
| `DATA_DIR` | `./data` | SQLite database (`app.db`) and the ESPN cache. Put this on a persistent volume. |
| `MAX_USERS` | 200 | Sign-ups stop at this many accounts. |

On Railway: create a service from this repo (it builds the `Dockerfile`), attach a volume mounted at `/data`, and
generate a domain.

### Command line

    python3 fantasy_tracker.py            # text summary for the leagues in leagues.json
    python3 fantasy_tracker.py --json     # dashboard data as JSON

`leagues.json` (gitignored):

    {"leagues": [{"name": "Work league", "sport": "football", "year": 2026, "league_id": 123456, "team_id": 4,
                  "espn_s2": "", "swid": ""}]}

### Offline testing

    python3 tests/make_fixture.py tests/fixture.json   # synthetic league shaped like ESPN's responses
    python3 fantasy_tracker.py --demo tests/fixture.json --print

## How accounts and data are handled

- Passwords are hashed with scrypt and a per-user salt. Sessions are random tokens in an `HttpOnly`,
  `SameSite=Lax` cookie; only their SHA-256 is stored.
- The database holds usernames, password hashes, sessions and each user's league ids, team ids and names.
  Nothing else.
- Sign-in and sign-up are rate-limited per IP. Each account can follow 12 leagues.
- "Delete account" on the Leagues page removes the account and its leagues.

## Sources

| Source | Used for |
|---|---|
| ESPN fantasy API (`lm-api-reads.fantasy.espn.com`) | League, rosters, projections, win probability, free agents, NFL schedule and byes |
| ESPN NFL scoreboard (`site.api.espn.com`) | Game state, clock, red zone, betting lines |
| Sleeper (`api.sleeper.app`) | Trending adds across Sleeper leagues |

None of these are official, documented APIs, and they can change without notice. This project isn't affiliated
with ESPN or Sleeper. Football only.

## License

MIT. See [LICENSE](LICENSE).
