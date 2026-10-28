"""The example trees, end to end, against recorded responses."""

import pytest

from pyrtan.tree import check


def test_examples_check_clean(examples):
    for tree in examples.trees():
        errors = [p for p in check(tree) if p.level == "error"]
        assert not errors, (tree.name, errors)


def test_top_story(runner):
    out = runner.run_intent("What's the top story on Hacker News?")
    assert out.status == "done"
    assert out.say == "Top of Hacker News: Pyrtan 0.1 — https://example.org/p"
    assert out.output["stories"][0]["by"] == "alice"


def test_top_n_stories_fills_in_self_post_url(runner):
    out = runner.run_intent("top 3 stories on hacker news")
    assert out.status == "done"
    stories = out.output["stories"]
    assert stories[1]["url"] == "https://news.ycombinator.com/item?id=102"
    assert stories[2] is None  # a comment is not a story: that item failed, the run did not
    assert any(s.node == "not-a-story" for s in out.trace.steps)


def test_count_is_bounded(runner):
    out = runner.run_intent("top 99 stories on hacker news")
    assert out.status == "error" and out.reason == "bad-input"


def test_item_missing(runner):
    out = runner.run_intent("hacker news item 999")
    assert out.status == "failed" and out.reason == "no-such-item"


def test_small_talk_is_one_decide_on_what_the_trigger_fixed(runner):
    for sentence, topic, starts in [("how are you doing?", "how", "Deterministic"), ("Are you human", "who", "A bot"), ("what can you do", "what", "Only what"), ("see ya", "bye", "Bye")]:
        out = runner.run_intent(sentence)
        assert out.status == "done" and out.tree == "small-talk" and out.output == {"topic": topic} and out.say.startswith(starts), sentence
        assert [s.node for s in out.trace.steps if s.kind != "intent"] == ["topic", topic]
    # A greeting alone is filler: it reads as nothing, and no tree answers it.
    assert runner.run_intent("hello").reason == "no-match"


def test_weather_two_hops(runner):
    out = runner.run_intent("weather in Oslo")
    assert out.status == "done"
    assert out.say == "Oslo, Norway: 4.2°C, raining, humidity 80%, wind 12.3 km/h."
    assert [s.chosen for s in out.trace.steps if s.kind == "decide"] == ["exists True", "type 'number'"]
    sky = next(s for s in out.trace.steps if s.kind == "lookup")
    assert (sky.node, sky.chosen, sky.observed, sky.next) == ("sky", "between [51, 67]", 61, "reply")


def test_weather_units_input(runner):
    out = runner.run_tree("weather", {"place": "Oslo", "units": "fahrenheit"})
    assert out.status == "done" and "39.6°F, clear" in out.say
    out = runner.run_tree("weather", {"place": "Oslo", "units": "kelvin"})
    assert out.status == "error" and out.reason == "bad-input"


def test_weather_unknown_place(runner):
    out = runner.run_intent("how cold is it in Nowhereville")
    assert out.status == "failed" and out.reason == "unknown-place"


def test_site_status_branches(runner):
    assert runner.run_intent("is Example.com up").status == "done"
    down = runner.run_intent("is broken.example up")
    assert down.status == "failed" and down.reason == "server-error"
    odd = runner.run_intent("check status of teapot.example")
    assert odd.status == "done" and "refuses" in odd.say


def test_site_status_rejects_bad_host(runner):
    # The {host:host} slot refuses what is not a host name, so the trigger
    # does not fire at all, and the reading says which trigger refused.
    out = runner.run_intent("is http://x up")
    assert out.status == "error" and out.reason == "no-match"
    assert out.output["reading"]["considered"] == ["is {host:host} up"]
    out = runner.run_tree("site-status", {"host": "http://x"})
    assert out.status == "error" and out.reason == "bad-input"  # the input's own pattern still guards a direct run


def test_github_release(runner):
    ok = runner.run_intent("latest release of b-i-t-8/pyrtan")
    assert ok.status == "done" and ok.output["tag"] == "v0.1.0"
    none = runner.run_intent("latest b-i-t-8/nothing release")
    assert none.status == "failed" and none.reason == "no-release"
    busy = runner.run_intent("latest release of b-i-t-8/busy")
    assert busy.reason == "rate-limited"


def test_fixture_fails_loudly_on_unforeseen_request(runner):
    out = runner.run_intent("weather in Paris")
    assert out.status == "error" and out.reason == "transport"


def test_disk_space_over_a_shell(runner):
    out = runner.run_intent("how full is the disk")
    assert out.status == "done" and out.say == "/ is 71% full." and out.output == {"mounted": "/", "percent": 71}
    assert out.trace.steps[1].summary == "shell df -P / -> exit 0; percent=71, mounted=/"
    assert runner.run_intent("disk space on /data").reason == "disk-full"
    failed = runner.run_intent("disk space on /nowhere")
    assert failed.status == "failed" and failed.reason == "df-failed" and "No such file" in failed.say


def test_log_errors_from_a_file(runner):
    out = runner.run_intent("errors in the log")
    assert out.status == "done" and out.say.startswith("2 error line(s) in the last 5 of /var/log/syslog; latest: ") and out.output["errors"] == 2
    assert runner.run_intent("errors in /var/log/quiet.log").say == "No error lines in the last 1 of /var/log/quiet.log."
    assert runner.run_intent("errors in /var/log/nothing.log").reason == "no-such-file"
    # The path from the sentence is checked against the tree's paths before anything is read.
    out = runner.run_intent("errors in /etc/passwd")
    assert out.status == "error" and out.reason == "path-not-allowed" and [s.kind for s in out.trace.steps] == ["intent"]


def test_room_temperature_from_a_queue(runner):
    assert runner.run_intent("temperature in the kitchen").say == "kitchen: 21.5°C."
    assert runner.run_intent("how warm is the attic").say == "attic: 31°C, warm."
    assert runner.run_intent("temperature in the garage").reason == "no-reading"
    gap = runner.run_intent("temperature in the hall")  # a payload that is not JSON: a gap, not a guess
    assert gap.status == "error" and gap.reason == "gap" and gap.trace.gaps[0]["node"] == "reading"
    assert runner.run_tree("room-temperature", {"room": "a-b"}).status == "error"  # fixture has nothing for it


def test_rendered_headline_through_a_browser(runner):
    out = runner.run_intent("headline of https://app.example/dashboard")
    assert out.status == "done" and out.say == "Dashboard — Welcome back, Barry" and out.output["heading"] == "Welcome back, Barry"
    assert out.trace.steps[1].summary == "browser https://app.example/dashboard -> 200; heading=Welcome back, Barry"
    assert runner.run_intent("what does https://app.example/empty say").reason == "no-heading"
    assert runner.run_intent("headline of https://app.example/gone").reason == "unexpected-response"


def test_modem_check_over_serial(runner):
    assert runner.run_intent("is the modem up").say == "The modem on /dev/ttyUSB0 is there."
    silent = runner.run_intent("is the modem on /dev/ttyUSB1 up")
    assert silent.status == "error" and silent.reason == "transport" and "no answer within 3 s" in silent.say
    assert runner.run_intent("is the modem on /dev/ttyACM0 up").reason == "modem-error"
    assert runner.run_intent("is the modem on /dev/ttyS0 up").reason == "device-not-allowed"


def test_capabilities_name_the_sources_and_lists(examples):
    caps = {t["name"]: t for t in examples.capabilities()}
    assert caps["disk-space"]["sources"] == ["shell"] and caps["disk-space"]["commands"] == ["df"] and "paths" not in caps["disk-space"]
    assert caps["room-temperature"]["sources"] == ["queue"] and caps["room-temperature"]["topics"] == ["home/+/temperature"]
    assert caps["weather"]["sources"] == ["http"] and caps["modem-check"]["devices"] == ["/dev/ttyUSB*", "/dev/ttyACM*"]


# ---------------------------------------------------------------- trees that run in a browser too
# Each reaches only an API that lets a page on another origin read it (CORS),
# so the console at pyrtan.com runs them in the page, with no hub.


def _path(out):
    return [s.node for s in out.trace.steps if s.kind != "intent"]


def test_wikipedia_summary_and_its_refusals(runner):
    out = runner.run_intent("tell me about Ursula K. Le Guin")
    assert out.status == "done" and out.inputs == {"topic": "Ursula K Le Guin"}
    assert out.say.startswith("Ursula K. Le Guin: Ursula Kroeber Le Guin was an American author.") and out.output["url"] == "https://en.wikipedia.org/wiki/Ursula_K._Le_Guin"
    out = runner.run_intent("wiki Mercury")
    assert out.reason == "ambiguous" and _path(out) == ["fetch", "status", "kind", "ambiguous"]
    assert runner.run_intent("who is Xyzzyplugh Notaperson").reason == "no-article"
    assert runner.run_intent("who was Alan Turing").reason == "rate-limited"


def test_define(runner):
    out = runner.run_intent("what does serendipity mean")
    assert out.status == "done" and out.say.startswith("serendipity (noun): A combination of events")
    assert out.output["phonetic"] == "/ˌsɛ.ɹən.ˈdɪ.pɪ.ti/"
    assert runner.run_intent("define zzzqx").reason == "unknown-word"


def test_sun_times_in_local_time(runner):
    out = runner.run_intent("when is sunset in Oslo")
    assert out.say == "Oslo, Norway, 2026-10-28: sunrise 07:28, sunset 16:32 (Europe/Oslo)."
    out = runner.run_intent("sunrise in Longyearbyen")  # polar night: no sunrise at all
    assert out.reason == "no-times" and _path(out)[-1] == "no-times"
    assert runner.run_intent("sun times for Nowhereville").reason == "unknown-place"


def test_air_quality_bands(runner):
    out = runner.run_intent("air quality in Oslo")
    assert out.say == "Oslo, Norway: air quality fair (European AQI 21), PM2.5 3.8 µg/m³, PM10 10.2 µg/m³."
    out = runner.run_intent("how is the air in Delhi")
    assert out.output["band"] == "very poor" and [s.chosen for s in out.trace.steps if s.node == "band"] == ["lte 100"]


def test_package_versions(runner):
    out = runner.run_intent("latest version of @types/node on npm")
    assert out.status == "done" and out.output == {"package": "@types/node", "version": "26.6.5", "description": "TypeScript definitions for node", "url": "https://www.npmjs.com/package/@types/node"}
    assert runner.run_intent("npm package nope-not-a-pkg-zz").reason == "no-package"
    out = runner.run_intent("latest requests on pypi")
    assert out.say == "requests 2.34.2 on PyPI: Python HTTP for Humans. https://pypi.org/project/requests/"
    assert runner.run_intent("pip version nope-not-a-pkg-zz").reason == "no-package"


def test_earthquakes(runner):
    out = runner.run_intent("any earthquakes today")
    assert out.output["count"] == 19 and out.output["latest"]["place"] == "7 km ENE of Pangyan, Philippines"
    assert out.say.startswith("19 earthquake(s) of magnitude 4.5+ in the past day. The latest: M5.1")


def test_iss_rounds_its_words_not_its_output(runner):
    out = runner.run_intent("where is the iss")
    assert out.say == "The ISS is over -26.62, -60.10, 422 km up, moving at 27577 km/h, in the Earth's shadow."
    assert out.output["latitude"] == -26.625662371377 and _path(out)[-2:] == ["in-shadow", "reply"]


def test_hn_search(runner):
    out = runner.run_intent("search hacker news for sqlite")
    assert out.output["total"] == 4632 and len(out.output["stories"]) == 3
    assert out.output["stories"][0]["discussion"] == "https://news.ycombinator.com/item?id=27016630"
    out = runner.run_intent("search hacker news for qqqzzzxxyyy")
    assert out.status == "done" and out.output == {"query": "qqqzzzxxyyy", "total": 0, "stories": []}


def test_books(runner):
    out = runner.run_intent("who wrote The Left Hand of Darkness")
    assert out.say == "The Left Hand of Darkness, by Ursula K. Le Guin (1969). https://openlibrary.org/works/OL59800W"
    assert runner.run_intent("find the book qqqzzzxxyyy").reason == "no-book"


def test_holidays_by_country_code(runner):
    out = runner.run_intent("next holiday in no")
    assert out.say == "The next public holiday in NO is Christmas Day (Første juledag) on 2026-12-25."
    assert out.output["after"] == {"date": "2026-12-26", "name": "St. Stephen's Day"}
    assert runner.run_intent("next holiday in XX").reason == "unknown-country"
    assert runner.run_intent("next holiday in Norway").reason == "bad-input"


def test_dad_joke_asks_for_json(runner, recorded):
    out = runner.run_intent("tell me a joke")
    assert out.status == "done" and out.say == "I am so good at sleeping I can do it with my eyes closed!"
    assert runner.registry.get("dad-joke").nodes["fetch"].get("http")["headers"] == {"Accept": "application/json"}


def test_exchange_rate_reads_one_rate_by_its_shape(runner):
    out = runner.run_intent("convert 100 usd to eur")
    assert out.say == "100 USD is 89.69 EUR at the ECB rate of 2026-10-27."
    assert out.output == {"amount": 100.0, "from": "USD", "to": "EUR", "value": 89.69, "date": "2026-10-27"}
    out = runner.run_intent("convert 1,000 EUR into JPY")  # the comma is the number's, not a pause
    assert out.inputs["amount"] == "1000" and out.say == "1000 EUR is 175970 JPY at the ECB rate of 2026-10-27."
    assert runner.run_intent("usd to xyz rate").reason == "unknown-currency"
    assert runner.run_intent("exchange rate from usd to usd").reason == "same-currency"
    assert runner.run_intent("convert -5 usd to eur").reason == "bad-input"


def test_synonyms_join_into_a_sentence(runner):
    out = runner.run_intent("synonyms for happy")
    assert out.say == "Synonyms for happy: halcyon, content, bright, felicitous, riant, expansive, elysian, joyful."
    assert out.output["synonyms"][:2] == ["halcyon", "content"]
    assert runner.run_intent("another word for qqqzzz").reason == "no-synonyms"


# ---------------------------------------------------------------- a second batch for the browser


def test_forecast_reads_tomorrow_not_today(runner):
    out = runner.run_intent("weather in Oslo tomorrow")
    assert out.tree == "forecast" and out.say == "Oslo, Norway, tomorrow (2026-10-29): rain, 5.9 to 9.4°C, 80% chance of rain."
    assert runner.run_intent("weather in Oslo").tree == "weather"  # today stays the weather tree's


def test_uv_index_band(runner):
    out = runner.run_intent("uv index in Sydney")
    assert out.output["band"] == "extreme" and out.say.endswith("stay out of the midday sun.")


def test_local_time_and_elevation_from_the_geocoder(runner):
    assert runner.run_intent("what time is it in La Paz").say == "It is 17:50 on Tuesday 2026-10-27 in La Paz, Mexico (America/Mazatlan)."
    assert runner.run_intent("how high is La Paz").say == "La Paz, Mexico is 33 m above sea level."
    assert runner.run_intent("how high is Oslo").reason == "no-height"  # this geocode carries no elevation


def test_postal_codes(runner):
    out = runner.run_intent("where is zip 90210")
    assert out.inputs == {"code": "90210", "country": "us"} and out.say == "90210 is Beverly Hills, California, United States."
    assert out.output["latitude"] == 34.0901
    assert runner.run_intent("postcode SW1A in GB").say == "SW1A is Westminster Abbey, England, Great Britain."
    assert runner.run_intent("where is zip 00000").reason == "unknown-code"


def test_wayback(runner):
    assert runner.run_intent("is example.com archived").output["url"].startswith("http://web.archive.org/web/")
    out = runner.run_intent("is nothing-here-qqzz.example archived")
    assert out.status == "done" and out.say == "The Wayback Machine has no copy of nothing-here-qqzz.example."


def test_rhymes_join(runner):
    assert runner.run_intent("what rhymes with cat").say == "Rhymes with cat: flat, caveat, brat, pat, combat, slat, that, rat, diplomat, fat."
    assert runner.run_intent("what rhymes with qqzzx").reason == "no-rhymes"


def test_github_repo(runner):
    out = runner.run_intent("how many stars does astral-sh/uv have")
    assert out.output["stars"] == 71234 and out.output["language"] == "Rust"
    assert runner.run_intent("github repo b-i-t-8/nothing-here").reason == "no-repo"
    assert runner.run_intent("github repo b-i-t-8/busy").reason == "rate-limited"


def test_package_managers(runner):
    assert runner.run_intent("latest version of github.com/gorilla/mux on go").say == "github.com/gorilla/mux v1.8.1, published 2023-10-18. https://pkg.go.dev/github.com/gorilla/mux"
    assert runner.run_intent("go module github.com/nope/nope-qq").reason == "no-module"
    out = runner.run_intent("brew formula wget")
    assert out.output["version"] == "1.25.0" and out.output["install"] == "brew install wget"
    assert runner.run_intent("brew formula nope-qqzz").reason == "no-formula"


def test_pokemon_types_join(runner):
    assert runner.run_intent("pokemon pikachu").say == "#25 Pikachu, electric type."
    out = runner.run_intent("what type is charizard")
    assert out.output["types"] == ["fire", "flying"] and out.output["height_dm"] == 17
    assert runner.run_intent("pokemon notapokemonqq").reason == "no-pokemon"


def test_small_apis(runner):
    assert runner.run_intent("what is my ip").output == {"ip": "203.0.113.7"}
    assert runner.run_intent("give me some advice").say.startswith("Take time once in a while")
    assert runner.run_intent("tell me a cat fact").say.startswith("A cat has more bones than a human")


def test_space(runner):
    out = runner.run_intent("when is the next launch")
    assert out.output == {"name": "Falcon 9 Block 5 | Starlink Group 15-18", "net_utc": "2026-10-30T04:02:00Z", "status": "Go for Launch", "upcoming": 474}
    out = runner.run_intent("space news")
    assert len(out.output["stories"]) == 3 and out.output["stories"][0]["site"] == "SpaceNews"
    out = runner.run_intent("astronomy picture of the day")
    assert out.output["title"] == "Day and Night over the Clouds" and out.output["media_type"] == "image"


# ---------------------------------------------------------------- a third batch for the browser


def test_antonyms_and_capitals(runner):
    assert runner.run_intent("opposite of happy").say == "The opposite of happy: sad, distressed, unhappy, dysphoric."
    assert runner.run_intent("antonyms for qqzzx").reason == "no-antonyms"
    out = runner.run_intent("what is the capital of Norway")
    assert out.say == "The capital of Norway is Oslo." and out.output["iso2"] == "NO"
    assert runner.run_intent("capital of Narnia").reason == "unknown-country"


def test_pollen_and_waves_say_where_their_models_end(runner):
    assert runner.run_intent("pollen in Paris").output["grass"] == 12.4
    assert runner.run_intent("pollen in Sydney").reason == "not-covered"
    assert runner.run_intent("waves in Lisbon").say == "Off Lisbon, Portugal: waves 1.42 m every 11.7 s, from 289°."
    assert runner.run_intent("waves in Munich").reason == "inland"


def test_crypto_price_by_name_or_ticker(runner):
    out = runner.run_intent("bitcoin price")
    assert out.inputs == {"coin": "BTC", "currency": "USD"} and out.output == {"coin": "BTC", "currency": "USD", "price": 80645.125}
    assert runner.run_intent("ethereum price in eur").say == "1 ETH is 2107.37 EUR (Coinbase spot price)."
    assert runner.run_intent("crypto price of QQQ").reason == "unknown-pair"


def test_package_downloads_and_nuget(runner):
    assert runner.run_intent("npm downloads for @types/node").output["downloads"] == 404009479
    assert runner.run_intent("npm downloads for nope-not-a-pkg-zz").reason == "no-package"
    assert runner.run_intent("latest version of Newtonsoft.Json on nuget").output["version"] == "14.0.1-beta2"
    assert runner.run_intent("nuget package nope-qqzz").reason == "no-package"


def test_dns_reads_the_answer_section_only(runner):
    out = runner.run_intent("mx record for gmail.com")
    assert out.output["type"] == "MX" and len(out.output["records"]) == 5 and "5 gmail-smtp-in.l.google.com." in out.output["records"]
    assert runner.run_intent("dns for example.com").output["records"] == ["172.66.147.243", "104.20.23.154"]
    assert runner.run_intent("dns for nope-qqzz.example").reason == "no-such-name"
    out = runner.run_intent("txt record for nope-qqzz-empty.pyrtan.com")
    assert out.status == "done" and out.output["records"] == []


def test_where_am_i(runner):
    assert runner.run_intent("where am i").output["city"] == "Columbus"


def test_quote_and_programming_joke(runner):
    assert runner.run_intent("give me a quote").output["author"] == "Rumi"
    out = runner.run_intent("tell me a programming joke")
    assert out.tree == "programming-joke" and out.say.startswith("Algorithm:")
    assert runner.run_intent("tell me a joke").tree == "dad-joke"


def test_food(runner):
    assert runner.run_intent("what is barcode 3017624010701").say == "3017624010701 is Nutella by Ferrero (400.0 g), Nutri-Score E."
    assert runner.run_intent("what is barcode 00000000").reason == "no-product"
    out = runner.run_intent("recipe for lasagna")  # search, then the first match by id: one recipe's ingredients
    assert out.output["dish"] == "Vegan Lasagna" and out.output["ingredients"][:2] == ["green red lentils", "Carrots"]
    assert [s.node for s in out.trace.steps if s.kind == "observe"] == ["search", "meal"]
    assert runner.run_intent("recipe for qqzzx").reason == "no-recipe"
    out = runner.run_intent("cocktail margarita")
    assert out.output["ingredients"] == ["Tequila", "Triple sec", "Lime juice", "Salt"] and out.output["glass"] == "Cocktail glass"
    assert runner.run_intent("cocktail qqzzx").reason == "no-drink"


def test_f1_events_and_people_in_space(runner):
    out = runner.run_intent("who won the last f1 race")
    assert out.say == "United States Grand Prix (2026-10-25): Andrea Kimi Antonelli won for Mercedes, then Leclerc (Ferrari) and Verstappen (Red Bull)."
    out = runner.run_intent("natural events")
    assert out.say == "NASA is tracking 5 open events. The latest: Tropical Cyclone Arnab." and len(out.output["events"]) == 5
    out = runner.run_intent("how many people are in space")
    assert out.output["count"] == 10 and out.output["people"][0] == "Zhu Yangzhu"
