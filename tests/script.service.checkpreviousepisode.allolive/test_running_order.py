"""Which episodes a check finds skipped, specials placed by release date or by the scraper."""
from running_order import find_gap, label, running_order

_ids = iter(range(1, 10000))


def ep(season, number, aired="", watched=False, **kw):
    e = dict(episodeid=next(_ids), season=season, episode=number, title="", firstaired=aired,
             playcount=1 if watched else 0, resume={"position": 0}, specialsortseason=-1,
             specialsortepisode=-1)
    e.update(kw)
    return e


def season(n, start_month, count, watched_upto=0):
    return [ep(n, i, "20%02d-%02d-%02d" % (10 + n, start_month, i), watched=i <= watched_upto)
            for i in range(1, count + 1)]


def names(episodes):
    return ["S%02dE%02d" % (e["season"], e["episode"]) for e in episodes]


def test_the_previous_episode_watched_is_no_gap():
    show = season(1, 3, 5, watched_upto=2)
    assert not find_gap(show, show[2]["episodeid"])


def test_an_unwatched_previous_episode_is_suggested():
    show = season(1, 3, 5, watched_upto=2)
    assert names(find_gap(show, show[4]["episodeid"]).missed) == ["S01E03", "S01E04"]


def test_a_special_released_between_two_episodes_runs_between_them():
    show = season(1, 3, 5, watched_upto=3) + [ep(0, 1, "2011-03-03")]   # S01E03 aired 2011-03-03 too
    assert names(find_gap(show, show[3]["episodeid"]).missed) == ["S00E01"]


def test_a_special_is_checked_against_what_aired_before_it():
    show = season(1, 3, 5, watched_upto=2) + [ep(0, 1, "2011-03-04")]
    assert names(find_gap(show, show[-1]["episodeid"]).missed) == ["S01E03", "S01E04"]


def test_the_end_of_one_season_is_checked_from_the_next():
    show = season(1, 3, 5, watched_upto=4) + season(2, 3, 5)
    assert names(find_gap(show, show[5]["episodeid"]).missed) == ["S01E05"]


def test_a_special_between_seasons_is_suggested_at_the_next_season():
    show = season(1, 3, 3, watched_upto=3) + [ep(0, 1, "2011-12-24")] + season(2, 3, 3)
    assert names(find_gap(show, show[4]["episodeid"]).missed) == ["S00E01"]


def test_the_scrapers_placement_wins_over_the_date():
    special = ep(0, 1, "2011-12-24", specialsortseason=1, specialsortepisode=2)
    show = season(1, 3, 3, watched_upto=1) + [special]
    assert [names(p)[0] for p in running_order(show)] == ["S01E01", "S00E01", "S01E02", "S01E03"]


def test_airs_after_a_season():
    special = ep(0, 1, "", specialsortseason=1, specialsortepisode=0x1000)
    show = season(1, 3, 2) + [special] + season(2, 3, 1)
    assert [names(p)[0] for p in running_order(show)] == ["S01E01", "S01E02", "S00E01", "S02E01"]


def test_a_special_without_a_real_date_is_left_out():
    show = season(1, 3, 3, watched_upto=1) + [ep(0, 1, "1969-12-31"), ep(0, 2, "")]
    assert names(find_gap(show, show[2]["episodeid"]).missed) == ["S01E02"]
    assert not find_gap(show, show[3]["episodeid"])      # nothing to check it against


def test_specials_can_be_turned_off():
    show = season(1, 3, 3, watched_upto=2) + [ep(0, 1, "2011-03-02")]
    assert names(find_gap(show, show[2]["episodeid"]).missed) == ["S00E01"]
    assert not find_gap(show, show[2]["episodeid"], specials=False)


def test_specials_released_before_the_show_do_not_stop_a_first_episode():
    show = [ep(0, 1, "1992-12-08"), ep(0, 2, "1995-12-01")] + season(1, 3, 3)
    assert not find_gap(show, show[2]["episodeid"])


def test_a_show_started_part_way_suggests_its_first_episode():
    show = [ep(0, 1, "1992-12-08")] + season(1, 3, 3)
    assert names(find_gap(show, show[3]["episodeid"]).missed) == ["S01E01", "S01E02"]


def test_a_gap_from_long_ago_still_counts():
    show = season(1, 3, 5, watched_upto=4)
    show[1]["playcount"] = 0                  # S01E02 skipped, S01E03 and S01E04 watched since
    assert names(find_gap(show, show[4]["episodeid"]).missed) == ["S01E02"]


def test_a_special_skipped_a_season_ago_is_suggested_with_the_new_gap():
    # Solar Opposites: S00E03 aired between seasons 3 and 4, S04E01-E05 watched, S04E06 not
    show = season(3, 7, 3, watched_upto=3) + [ep(0, 3, "2013-10-03")] + season(4, 8, 7, watched_upto=5)
    playing = next(e for e in show if (e["season"], e["episode"]) == (4, 7))
    assert names(find_gap(show, playing["episodeid"]).missed) == ["S00E03", "S04E06"]


def test_an_episode_missing_from_the_library_is_reported():
    show = [e for e in season(1, 3, 5, watched_upto=2) if e["episode"] != 4]
    gap = find_gap(show, show[3]["episodeid"])                # S01E05
    assert names(gap.missed) == ["S01E03"] and gap.absent == [(1, 4)]


def test_a_second_copy_of_an_episode_watched_counts():
    show = season(1, 3, 3, watched_upto=1)
    show.append(ep(1, 2, "2011-03-02", watched=True))         # a second file of S01E02, watched
    assert not find_gap(show, show[2]["episodeid"])


def test_label():
    assert label(ep(0, 3, "2011-09-15", title="Heart of Archness (1)")) == \
        'S00E03 "Heart of Archness (1)" (2011-09-15)'


def test_a_special_from_before_1970_is_placed():
    show = [ep(1, n, "1966-09-%02d" % (n * 7), watched=n == 1) for n in (1, 2, 3)] + [ep(0, 1, "1966-09-15")]
    assert names(find_gap(show, show[2]["episodeid"]).missed) == ["S01E02", "S00E01"]
