"""Console entry points with consistent Ctrl+C handling."""

import importlib
import sys
from collections.abc import Callable
from typing import cast


class ConsoleScript:
    def __init__(self, module: str) -> None:
        self.module = module

    def __call__(self) -> int | None:
        try:
            main = cast(Callable[[], int | None], importlib.import_module(self.module).main)
            return main()
        except KeyboardInterrupt:
            print("Interrupted by user; exiting", file=sys.stderr)
            return 130


mediascripts = ConsoleScript("mediascripts.__main__")
acoustid = ConsoleScript("mediascripts.acoustid.acoustid")
musicbrainz = ConsoleScript("mediascripts.acoustid.musicbrainz")
spotify = ConsoleScript("mediascripts.spotify.spotify")
artist_countries_to_mapgraph_json = ConsoleScript("mediascripts.artist.artist_mapgraph.artist_countries_to_mapgraph_json")
artist_yaml_auto_populate = ConsoleScript("mediascripts.artist.artist_yaml.artist_yaml_auto_populate")
artist_yaml_reformat = ConsoleScript("mediascripts.artist.artist_yaml.artist_yaml_reformat")
artist_yaml_test = ConsoleScript("mediascripts.artist.artist_yaml.artist_yaml_test")
auto_tag_media_file = ConsoleScript("mediascripts.mediafile.auto_tag_media_file")
track_yaml_auto_populate = ConsoleScript("mediascripts.track.track_yaml.track_yaml_auto_populate")
track_yaml_auto_create = ConsoleScript("mediascripts.track.track_yaml.track_yaml_auto_create")
track_yaml_reformat = ConsoleScript("mediascripts.track.track_yaml.track_yaml_reformat")
scan_media_files_to_track_yaml = ConsoleScript("mediascripts.track.track_yaml.scan_media_files_to_track_yaml")
convert_covers = ConsoleScript("mediascripts.convert.convert_covers")
cover_images_auto_download = ConsoleScript("mediascripts.cover.cover_images_auto_download")
convert_videos = ConsoleScript("mediascripts.convert.convert_videos")
compare_track_yaml_with_media_file = ConsoleScript("mediascripts.track.track_yaml.compare_track_yaml_with_media_file")
merge_track_yaml_with_media_file = ConsoleScript("mediascripts.track.track_yaml.merge_track_yaml_with_media_file")
copy_medialib = ConsoleScript("mediascripts.copy.copy_medialib")
generate_dataclasses = ConsoleScript("mediascripts.dev.generate_dataclasses")
genre_clusters = ConsoleScript("mediascripts.stats.genre_clusters")
list_albums = ConsoleScript("mediascripts.mediascan_db.list_albums")
list_artists = ConsoleScript("mediascripts.mediascan_db.list_artists")
list_mediafiles = ConsoleScript("mediascripts.mediascan_db.list_mediafiles")
list_tracks = ConsoleScript("mediascripts.mediascan_db.list_tracks")
list_trackstreaming = ConsoleScript("mediascripts.mediascan_db.list_trackstreaming")
lyrics_text_auto_populate = ConsoleScript("mediascripts.lyrics.lyrics_text.lyrics_text_auto_populate")
lyrics_text_auto_populate_from_file = ConsoleScript("mediascripts.lyrics.lyrics_text.lyrics_text_auto_populate_from_file")
lyrics_text_auto_populate_from_lrclib = ConsoleScript("mediascripts.lyrics.lyrics_text.lyrics_text_auto_populate_from_lrclib")
lyrics_lrc_auto_populate = ConsoleScript("mediascripts.lyrics.lyrics_lrc.lyrics_lrc_auto_populate")
list_missing_region_names = ConsoleScript("mediascripts.artist.artist_mapgraph.list_missing_region_names")
make_covers_video = ConsoleScript("mediascripts.convert.make_covers_video")
rename_album_files = ConsoleScript("mediascripts.rename.rename_album_files")
