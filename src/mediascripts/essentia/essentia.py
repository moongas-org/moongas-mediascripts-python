"""Extract aggregate MFCC features from an audio file."""

import argparse
from pathlib import Path


def extract_features(audio_file: Path, output_file: Path) -> None:
    import essentia
    import essentia.standard as es

    audio = es.MonoLoader(filename=str(audio_file))()
    pool = essentia.Pool()
    windowing = es.Windowing(type="hann")
    spectrum = es.Spectrum()
    mfcc = es.MFCC()

    for frame in es.FrameGenerator(audio, frameSize=1024, hopSize=512):
        spec = spectrum(windowing(frame))
        _, mfcc_coeffs = mfcc(spec)
        pool.add("lowlevel.mfcc", mfcc_coeffs)

    aggregated_pool = es.PoolAggregator(defaultStats=["mean", "var"])(pool)
    es.YamlOutput(filename=str(output_file), format="json")(aggregated_pool)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_file", type=Path, help="audio file to analyze")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("features.json"),
        help="JSON output file (default: features.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extract_features(args.audio_file, args.output)
    print(f"Features successfully extracted and saved to {args.output}!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
