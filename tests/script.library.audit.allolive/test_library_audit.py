"""script.library.audit.allolive: duplicate detection, the keeper choice, the deletion
plan's safety rules, dry run, and the orphan scan."""
import os


def movie(file, movieid=1, title="Movie", year=2001, uniqueid=None, height=1080, width=1920,
          playcount=0, dateadded="2026-01-01 00:00:00"):
    return {"movieid": movieid, "title": title, "year": year, "file": file,
            "uniqueid": uniqueid or {}, "imdbnumber": "", "playcount": playcount,
            "dateadded": dateadded,
            "streamdetails": {"video": [{"height": height, "width": width, "codec": "hevc"}]}}


# -- duplicates -----------------------------------------------------------------------------

def test_movie_key_prefers_imdb_then_tmdb_then_title_and_year(audit):
    assert audit.movie_key({"uniqueid": {"tmdb": "7", "imdb": "tt1"}}) == "imdb:tt1"
    assert audit.movie_key({"uniqueid": {"tmdb": "7"}}) == "tmdb:7"
    assert audit.movie_key({"imdbnumber": " tt9 "}) == "imdb:tt9"
    assert audit.movie_key({"title": "Le Négociateur!", "year": 2024}) == "ty:lengociateur:2024"
    assert audit.movie_key({"title": ""}) is None


def test_find_duplicates_groups_same_movie_only(audit):
    a1 = movie("/m/a1.mkv", 1, "Alpha", uniqueid={"imdb": "tt1"})
    a2 = movie("/m/a2.mkv", 2, "Alpha", uniqueid={"imdb": "tt1"})
    b = movie("/m/b.mkv", 3, "Beta", uniqueid={"imdb": "tt2"})
    z1 = movie("/m/z1.mkv", 4, "Zeta", uniqueid={"tmdb": "9"})
    z2 = movie("/m/z2.mkv", 5, "Zeta", uniqueid={"tmdb": "9"})
    groups = audit.find_duplicates([z1, b, a1, z2, a2])
    assert [[m["movieid"] for m in g] for g in groups] == [[1, 2], [4, 5]]


def test_keeper_is_highest_resolution_then_largest_file(audit, tree):
    root = tree({"a.mkv": 10, "b.mkv": 20, "c.mkv": 5})
    uhd_small = movie(root + "c.mkv", 1, height=2160, width=3840)
    hd_big = movie(root + "b.mkv", 2)
    hd_small = movie(root + "a.mkv", 3)
    assert audit.recommended_keeper_index([hd_small, uhd_small, hd_big]) == 1
    assert audit.recommended_keeper_index([hd_small, hd_big]) == 1


# -- deletion plan: when a whole folder may go ---------------------------------------------

def test_folder_removed_when_nothing_else_lives_there(audit, tree):
    root = tree({"Movies/A (2001)/A.mkv": 1, "Movies/A (2001)/A.nfo": 1})
    loser = movie(root + "Movies/A (2001)/A.mkv")
    plan = audit.deletion_plan([loser], {loser["file"]}, [{"file": root + "Movies/"}])
    assert [(kind, target) for kind, target, _, _ in plan] == [("folder", root + "Movies/A (2001)/")]


def test_only_the_file_when_another_library_movie_shares_the_folder(audit, tree):
    root = tree({"Movies/Mixed/A.mkv": 1, "Movies/Mixed/B.mkv": 1})
    loser = movie(root + "Movies/Mixed/A.mkv")
    library = {loser["file"], root + "Movies/Mixed/B.mkv"}
    plan = audit.deletion_plan([loser], library, [{"file": root + "Movies/"}])
    assert plan == [("file", loser["file"], [loser], "other library files remain in folder")]


def test_never_the_source_root_itself(audit, tree):
    root = tree({"Movies/A.mkv": 1})
    loser = movie(root + "Movies/A.mkv")
    plan = audit.deletion_plan([loser], {loser["file"]}, [{"file": root + "Movies"}])
    assert plan == [("file", loser["file"], [loser], "folder is a source root")]


def test_only_the_file_when_a_subfolder_holds_another_video(audit, tree):
    root = tree({"Movies/Saga/Part1.mkv": 1, "Movies/Saga/Part2/Part2.mkv": 1})
    loser = movie(root + "Movies/Saga/Part1.mkv")
    plan = audit.deletion_plan([loser], {loser["file"]}, [{"file": root + "Movies/"}])
    assert plan == [("file", loser["file"], [loser], "subfolder(s) contain other videos")]


def test_extras_and_disc_structure_do_not_block_the_folder(audit, tree):
    root = tree({"Movies/A/A.mkv": 1, "Movies/A/Extras/making-of.mkv": 1})
    loser = movie(root + "Movies/A/A.mkv")
    plan = audit.deletion_plan([loser], {loser["file"]}, [{"file": root + "Movies/"}])
    assert [(k, t) for k, t, _, _ in plan] == [("folder", root + "Movies/A/")]


def test_bluray_rip_removes_the_movie_folder_not_bdmv(audit, tree):
    root = tree({"Movies/B/BDMV/index.bdmv": 1, "Movies/B/BDMV/STREAM/00001.m2ts": 1})
    loser = movie(root + "Movies/B/BDMV/index.bdmv")
    plan = audit.deletion_plan([loser], {loser["file"]}, [{"file": root + "Movies/"}])
    assert [(k, t) for k, t, _, _ in plan] == [("folder", root + "Movies/B/")]


def test_stacked_movie_is_deleted_part_by_part(audit):
    loser = movie("stack:///m/A-cd1.avi , /m/A-cd2.avi")
    plan = audit.deletion_plan([loser], set(), [{"file": "/m/"}])
    assert plan == [("file", loser["file"], [loser], "stack path")]


# -- dry run and live -------------------------------------------------------------------------

def test_dry_run_is_the_default_and_deletes_nothing(audit, kodi, tree):
    root = tree({"Movies/A/A.mkv": 1, "Movies/B/B.mkv": 1})
    a, b = movie(root + "Movies/A/A.mkv", 1), movie(root + "Movies/B/B.mkv", 2)
    plan = [("folder", root + "Movies/A/", [a], "r"), ("file", b["file"], [b], "r")]
    assert audit.is_dry_run()
    audit.apply_deletion_plan(plan)
    assert os.path.exists(a["file"]) and os.path.exists(b["file"])
    assert kodi.calls("VideoLibrary.RemoveMovie") == []
    shown = audit.xbmcgui.Dialog.shown[-1]
    assert "NOT executed" in shown and "rmdir" in shown and b["file"] in shown


def test_delete_backstop_holds_in_dry_run(audit, tree):
    root = tree({"A.mkv": 1})
    assert audit.delete_file_path(root + "A.mkv") is True
    assert audit._rmdir_force(root) is True
    assert os.path.exists(root + "A.mkv")


def test_live_run_removes_library_entries_files_and_folders(audit, kodi, tree):
    kodi.settings["dry_run"] = False
    root = tree({"Movies/A/A.mkv": 1, "Movies/A/A.nfo": 1, "Movies/B/B.mkv": 1, "Movies/B/C.mkv": 1})
    a, b = movie(root + "Movies/A/A.mkv", 1), movie(root + "Movies/B/B.mkv", 2)
    audit.apply_deletion_plan([("folder", root + "Movies/A/", [a], "r"),
                               ("file", b["file"], [b], "r")])
    assert not os.path.exists(root + "Movies/A")
    assert not os.path.exists(b["file"]) and os.path.exists(root + "Movies/B/C.mkv")
    assert kodi.calls("VideoLibrary.RemoveMovie") == [{"movieid": 1}, {"movieid": 2}]


# -- orphans ------------------------------------------------------------------------------------

def test_orphan_scan_reports_videos_the_library_does_not_know(audit, kodi, tree):
    root = tree({"Movies/A/A.mkv": 1, "Movies/A/A.nfo": 1, "Movies/A/A-sample.mkv": 1,
                 "Movies/B/B.mp4": 1, "Movies/C/VIDEO_TS/VTS_01_1.VOB": 1,
                 "Movies/D/Trailers/t.mkv": 1})
    kodi.movies = [{"file": root + "Movies/A/A.mkv"}]
    assert audit.find_orphan_files([{"file": root + "Movies/"}]) == [root + "Movies/B/B.mp4"]


def test_orphan_scan_expands_multipath_sources(audit, kodi, tree):
    root = tree({"One/A.mkv": 1, "Two/B.mkv": 1})
    multipath = "multipath://%s/%s/" % ((root + "One/").replace("/", "%2f"),
                                         (root + "Two/").replace("/", "%2f"))
    assert audit.expand_source(multipath) == [root + "One/", root + "Two/"]
    assert audit.find_orphan_files([{"file": multipath}]) == [root + "One/A.mkv", root + "Two/B.mkv"]


def test_unlistable_folder_counts_as_holding_videos(audit, tmp_path):
    assert audit._has_nested_video(str(tmp_path / "missing") + "/") is True


def test_human_size(audit):
    assert [audit.human_size(n) for n in (None, 512, 2048, 5 * 1024 ** 3)] == [
        "?", "512 B", "2.0 KB", "5.0 GB"]
