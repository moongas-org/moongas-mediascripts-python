import argparse

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans

from mediascan.genres import Genre

"""
Note: I used this script to generate some of the genre cluster links
It's only based on the genre name (e.g. "Classic Rock"), not the quality 
of the music, so it's not very good, e.g. it groups "Glam Rock" with "Goth Rock", 
so some manual editing was still required. Otherwise this code could be 
integrated into the app. Nonetheless the script was very helpful and could
easily be repurposed for myriad other uses.
TODO: Research more advanced forms of genre classification and introduce
genre tag-checking or auto-tagging feature.
"""


def main():
    parser = argparse.ArgumentParser(
        description="Generates genre clusters based on genre names."
    )
    parser.add_argument(
        "-n",
        "--n-clusters",
        type=int,
        default=16,
        help="number of KMeans clusters (default: %(default)s)",
    )
    args = parser.parse_args()

    # List of genres
    genres: list[str] = [str(genre) for genre in Genre]

    # Convert genres into numerical vectors using TF-IDF
    vectorizer = TfidfVectorizer(stop_words="english")
    X = vectorizer.fit_transform(genres)  # pyright: ignore[reportUnknownMemberType]

    n_clusters: int = args.n_clusters
    # Fit the KMeans clustering algorithm
    kmeans = KMeans(n_clusters=n_clusters, random_state=42)
    kmeans.fit(X)

    # Get the clusters assigned to each genre
    clusters = kmeans.labels_

    # Create a dictionary to map genres to their clusters
    clustered_genres: dict[int, list[str]] = {i: [] for i in range(n_clusters)}
    for genre, cluster in zip(genres, clusters):
        clustered_genres[cluster].append(genre)

    # Print the clustered genres
    for cluster, cluster_genres in clustered_genres.items():
        print(f"Cluster {cluster}:")
        print("[" + ", ".join([f"'{c}'" for c in cluster_genres]) + "]")
        print()


if __name__ == "__main__":
    main()
