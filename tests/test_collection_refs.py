import unittest

from core.collection_refs import repair_collection_refs, resolve_library_track_ref


LIBRARY = [
    {
        "artist": "Artist",
        "albums": [
            {
                "title": "02 - Current Album",
                "album_id": "new-id",
                "tracks": [
                    {
                        "title": "01 - Song",
                        "url": "/music/Artist/Current%20Album/01%20-%20Song.mp3",
                        "album_id": "new-id",
                    }
                ],
            }
        ],
    }
]


class CollectionRefRepairTests(unittest.TestCase):
    def test_stale_album_id_is_repaired_by_valid_url(self):
        stale = {
            "url": "/music/Artist/Current%20Album/01%20-%20Song.mp3",
            "artist_name": "Artist",
            "album_title": "01 - Old Album",
            "track_title": "01 - Song",
            "album_id": "old-id",
        }
        repaired, removed, changed = repair_collection_refs([stale], LIBRARY)
        self.assertTrue(changed)
        self.assertEqual(removed, 0)
        self.assertEqual(repaired[0]["album_id"], "new-id")
        self.assertEqual(repaired[0]["album_title"], "02 - Current Album")

    def test_stable_id_and_title_survive_url_rename(self):
        stale_url = {
            "url": "/music/Artist/Old/01%20-%20Song.mp3",
            "artist_name": "Artist",
            "album_title": "Old",
            "track_title": "01 - Song",
            "album_id": "new-id",
        }
        match = resolve_library_track_ref(stale_url, LIBRARY)
        self.assertIsNotNone(match)
        self.assertEqual(match[0]["url"], LIBRARY[0]["albums"][0]["tracks"][0]["url"])

    def test_genuinely_missing_and_duplicate_refs_are_removed(self):
        valid = {
            "url": "/music/Artist/Current%20Album/01%20-%20Song.mp3",
            "artist_name": "Artist",
            "album_title": "02 - Current Album",
            "track_title": "01 - Song",
            "album_id": "new-id",
        }
        missing = {
            "url": "/music/Gone.mp3",
            "artist_name": "Gone",
            "album_title": "Gone",
            "track_title": "Gone",
            "album_id": "gone-id",
        }
        repaired, removed, changed = repair_collection_refs([valid, valid, missing], LIBRARY)
        self.assertTrue(changed)
        self.assertEqual(removed, 2)
        self.assertEqual(repaired, [valid])

    def test_valid_youtube_ref_is_kept_without_library_match(self):
        youtube = {
            "_is_youtube": True,
            "url": "https://www.youtube.com/watch?v=abc",
            "track_title": "Video",
            "artist_name": "Channel",
            "album_title": "Video",
            "album_id": "stale-value",
        }
        repaired, removed, changed = repair_collection_refs([youtube], [])
        self.assertTrue(changed)
        self.assertEqual(removed, 0)
        self.assertEqual(repaired[0]["album_id"], "")


if __name__ == "__main__":
    unittest.main()
