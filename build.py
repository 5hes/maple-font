import argparse
import hashlib
import importlib.util
import json
import shutil
import time
from functools import partial
from multiprocessing import Pool
from os import environ, listdir, makedirs, path, remove, walk, getenv
from typing import Callable
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile
from fontTools.ttLib import TTFont, newTable
from fontTools.merge import Merger
from source.py.utils import get_font_forge_bin, get_font_name, run, set_font_name
from source.py.feature import freeze_feature

# =========================================================================================


def check_ftcli():
    package_name = "foundryToolsCLI"
    package_installed = importlib.util.find_spec(package_name) is not None

    if not package_installed:
        print(f"{package_name} is not found. Please run `pip install foundrytools-cli`")
        exit(1)


# =========================================================================================


def parse_args():
    parser = argparse.ArgumentParser(description="Optimizer and builder for Maple Mono")
    parser.add_argument(
        "--prefix",
        type=str,
        help="Output directory prefix",
    )
    parser.add_argument(
        "--normal",
        action="store_true",
        help="Whether to use normal preset",
    )
    parser.add_argument(
        "--cn-both",
        action="store_true",
        help="Whether to build `Maple Mono CN` and `Maple Mono NF CN`",
    )
    parser.add_argument(
        "--cn-narrow",
        action="store_true",
        help="Whether to make CN characters narrow",
    )
    parser.add_argument(
        "--hinted",
        action="store_true",
        help="Whether to use hinted font as base font",
    )
    parser.add_argument(
        "--no-hinted",
        action="store_false",
        help="Whether not to use hinted font as base font",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Whether to archieve fonts",
    )

    return parser.parse_args()


# =========================================================================================


class FontConfig:
    def __init__(self):
        self.release_mode = None
        self.use_cn_both = None
        self.dir_prefix = None
        # the number of parallel tasks
        # when run in codespace, this will be 1
        self.pool_size = 1 if not getenv("CODESPACE_NAME") else 4
        # font family name
        self.family_name = "Maple Mono"
        self.family_name_compact = "MapleMono"
        # whether to use hinted ttf as base font
        self.use_hinted = True
        self.feature_freeze = {
            "cv01": "ignore",
            "cv02": "ignore",
            "cv03": "ignore",
            "cv04": "ignore",
            "cv31": "ignore",
            "cv32": "ignore",
            "cv33": "ignore",
            "cv34": "ignore",
            "cv35": "ignore",
            "cv36": "ignore",
            "cv98": "ignore",
            "cv99": "ignore",
            "ss01": "ignore",
            "ss02": "ignore",
            "ss03": "ignore",
            "ss04": "ignore",
            "ss05": "ignore",
            "ss06": "ignore",
            "ss07": "ignore",
            "zero": "ignore",
        }
        # nerd font settings
        self.nerd_font = {
            # whether to enable Nerd Font
            "enable": True,
            # target version of Nerd Font if font-patcher not exists
            "version": "3.2.1",
            # whether to make icon width fixed
            "mono": False,
            # prefer to use Font Patcher instead of using prebuild NerdFont base font
            # if you want to custom build nerd font using font-patcher, you need to set this to True
            "use_font_patcher": False,
            # symbol Fonts settings.
            # default args: ["--complete"]
            # if not, will use font-patcher to generate fonts
            # full args: https://github.com/ryanoasis/nerd-fonts?tab=readme-ov-file#font-patcher
            "glyphs": ["--complete"],
            # extra args for font-patcher
            # default args: ["-l", "--careful", "--outputdir", output_nf]
            # if "mono" is set to True, "--mono" will be added
            # full args: https://github.com/ryanoasis/nerd-fonts?tab=readme-ov-file#font-patcher
            "extra_args": [],
        }
        # chinese font settings
        self.cn = {
            # whether to build Chinese fonts
            # skip if Chinese base fonts are not founded
            "enable": True,
            # whether to patch Nerd Font
            "with_nerd_font": True,
            # fix design language and supported languages
            "fix_meta_table": True,
            # whether to clean instantiated base CN fonts
            "clean_cache": False,
            # whether to narrow CN glyphs
            "narrow": False,
            # whether to hint CN font (will increase about 33% size)
            "use_hinted": False,
        }

    def load_external(self, args):
        self.release_mode = args.release
        self.use_cn_both = args.cn_both
        self.dir_prefix = args.prefix

        try:
            with open(
                "./source/preset-normal.json" if args.normal else "config.json", "r"
            ) as f:
                data = json.load(f)
                self.family_name = data["family_name"]
                self.use_hinted = data["use_hinted"]
                self.pool_size = data["pool_size"]
                self.feature_freeze = data["feature_freeze"]
                self.nerd_font = data["nerd_font"]
                self.cn = data["cn"]

                self.family_name_compact = self.family_name.replace(" ", "")

                if "font_forge_bin" not in self.nerd_font:
                    self.nerd_font["font_forge_bin"] = get_font_forge_bin()

                if "github_mirror" not in self.nerd_font:
                    self.nerd_font["github_mirror"] = "github.com"

                if args.hinted is not None:
                    self.use_hinted = True

                if args.no_hinted is not None:
                    self.use_hinted = False

                if args.cn_narrow is not None:
                    self.cn["narrow"] = True

        except ():
            print("Fail to load config.json. Please check your config.json.")
            exit(1)

    def should_use_font_patcher(self):
        if not (
            len(self.nerd_font["extra_args"]) > 0
            or self.nerd_font["use_font_patcher"]
            or self.nerd_font["glyphs"] != ["--complete"]
        ):
            return False

        if check_font_patcher(
            version=self.nerd_font["version"],
            github_mirror=self.nerd_font["github_mirror"],
        ) and not path.exists(self.nerd_font["font_forge_bin"]):
            print(
                f"FontForge bin({self.nerd_font['font_forge_bin']}) not found. Use prebuild Nerd Font instead."
            )
            return False

        return True


class BuildOption:
    def __init__(self, config: FontConfig):
        # paths
        self.src_dir = "source"
        self.output_dir_default = "fonts"
        self.output_dir = (
            path.join(self.output_dir_default, config.dir_prefix)
            if config.dir_prefix
            else self.output_dir_default
        )
        self.output_otf = path.join(self.output_dir, "OTF")
        self.output_ttf = path.join(self.output_dir, "TTF")
        self.output_ttf_hinted = path.join(self.output_dir, "TTF-AutoHint")
        self.output_variable = path.join(self.output_dir_default, "Variable")
        self.output_woff2 = path.join(self.output_dir, "Woff2")
        self.output_nf = path.join(self.output_dir, "NF")
        self.ttf_base_dir = path.join(
            self.output_dir, "TTF-AutoHint" if config.use_hinted else "TTF"
        )

        self.cn_static_path = f"{self.src_dir}/cn/static"

        self.cn_base_font_dir = None
        self.cn_suffix = None
        self.cn_suffix_compact = None
        self.output_cn = None
        # In these subfamilies:
        #   - NameID1 should be the family name
        #   - NameID2 should be the subfamily name
        #   - NameID16 and NameID17 should be removed
        # Other subfamilies:
        #   - NameID1 should be the family name, append with subfamily name without "Italic"
        #   - NameID2 should be the "Regular" or "Italic"
        #   - NameID16 should be the family name
        #   - NameID17 should be the subfamily name
        # https://github.com/subframe7536/maple-font/issues/182
        # https://github.com/subframe7536/maple-font/issues/183
        #
        # same as `ftcli assistant commit . --ls 400 700`
        # https://github.com/ftCLI/FoundryTools-CLI/issues/166#issuecomment-2095756721
        self.skip_subfamily_list = ["Regular", "Bold", "Italic", "BoldItalic"]

    def load_cn_dir_and_suffix(self, with_nerd_font: bool):
        if with_nerd_font:
            self.cn_base_font_dir = self.output_nf
            self.cn_suffix = "NF CN"
            self.cn_suffix_compact = "NF-CN"
        else:
            self.cn_base_font_dir = path.join(self.output_dir, "TTF")
            self.cn_suffix = self.cn_suffix_compact = "CN"
        self.output_cn = path.join(self.output_dir, self.cn_suffix_compact)


def freeze(font: TTFont, freeze_config: dict[str, str]):
    """
    freeze font features
    """
    freeze_feature(
        font=font,
        moving_rules=["ss03", "ss05", "ss07"],
        config=freeze_config,
    )


def check_font_patcher(version: str, github_mirror: str) -> bool:
    if path.exists("FontPatcher"):
        with open("FontPatcher/font-patcher", "r", encoding="utf-8") as f:
            if f"# Nerd Fonts Version: {version}" in f.read():
                return True
            else:
                print("FontPatcher version not match, delete it")
                shutil.rmtree("FontPatcher", ignore_errors=True)

    zip_path = "FontPatcher.zip"
    if not path.exists(zip_path):
        github = environ.get("GITHUB")  # custom github mirror, for CN users
        if not github:
            github = github_mirror
        url = f"https://{github}/ryanoasis/nerd-fonts/releases/download/v{version}/FontPatcher.zip"
        try:
            print(f"NerdFont Patcher does not exist, download from {url}")
            with urlopen(url) as response, open(zip_path, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
        except Exception as e:
            print(
                f"\nFail to download NerdFont Patcher. Please download it manually from {url}, then put downloaded 'FontPatcher.zip' into project's root and run this script again. \n\tError: {e}"
            )
            exit(1)

    with ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall("FontPatcher")
    remove(zip_path)
    return True


def parse_font_name(style_name_compact: str, skip_subfamily_list: list[str]):
    is_italic = style_name_compact.endswith("Italic")

    _style_name = style_name_compact
    if is_italic and style_name_compact[0] != "I":
        _style_name = style_name_compact[:-6] + " Italic"

    if style_name_compact in skip_subfamily_list:
        return "", _style_name, _style_name, is_italic
    else:
        return (
            " " + style_name_compact.replace("Italic", ""),
            "Italic" if is_italic else "Regular",
            _style_name,
            is_italic,
        )


def fix_cv98(font: TTFont):
    gsub_table = font["GSUB"].table
    feature_list = gsub_table.FeatureList

    for feature_record in feature_list.FeatureRecord:
        if feature_record.FeatureTag != "cv98":
            continue
        sub_table = gsub_table.LookupList.Lookup[
            feature_record.Feature.LookupListIndex[0]
        ].SubTable[0]
        sub_table.mapping = {
            "emdash": "emdash.cv98",
            "ellipsis": "ellipsis.cv98",
        }
        break


def remove_locl(font: TTFont):
    gsub = font["GSUB"]
    features_to_remove = []

    for feature in gsub.table.FeatureList.FeatureRecord:
        feature_tag = feature.FeatureTag

        if feature_tag == "locl":
            features_to_remove.append(feature)

    for feature in features_to_remove:
        gsub.table.FeatureList.FeatureRecord.remove(feature)


def get_unique_identifier(
    postscript_name: str, freeze_config: dict[str, str], narrow: bool = False, suffix=""
) -> str:
    if suffix == "":
        for k, v in freeze_config.items():
            if v == "enable":
                suffix += f"+{k};"
            elif v == "disable":
                suffix += f"-{k};"

    if "CN" in postscript_name and narrow:
        suffix = "Narrow;" + suffix

    return f"Version 7.000;SUBF;{postscript_name};2024;FL830;{suffix}"


def change_char_width(font: TTFont, match_width: int, target_width: int):
    font["hhea"].advanceWidthMax = target_width
    for name in font.getGlyphOrder():
        glyph = font["glyf"][name]
        width, lsb = font["hmtx"][name]
        if width != match_width:
            continue
        if glyph.numberOfContours == 0:
            font["hmtx"][name] = (target_width, lsb)
            continue

        delta = round((target_width - width) / 2)
        glyph.coordinates.translate((delta, 0))
        glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax = (
            glyph.coordinates.calcIntBounds()
        )
        font["hmtx"][name] = (target_width, lsb + delta)


def build_mono(f: str, font_config: FontConfig, build_option: BuildOption):
    _path = path.join(build_option.output_ttf, f)
    font = TTFont(_path)

    style_compact = f.split("-")[-1].split(".")[0]

    style_with_prefix_space, style_in_2, style, _ = parse_font_name(
        style_name_compact=style_compact,
        skip_subfamily_list=build_option.skip_subfamily_list,
    )

    set_font_name(
        font,
        font_config.family_name + style_with_prefix_space,
        1,
    )
    set_font_name(font, style_in_2, 2)
    set_font_name(
        font,
        f"{font_config.family_name} {style}",
        4,
    )
    postscript_name = f"{font_config.family_name_compact}-{style_compact}"
    set_font_name(font, postscript_name, 6)
    set_font_name(
        font,
        get_unique_identifier(
            postscript_name=postscript_name,
            freeze_config=font_config.feature_freeze,
        ),
        3,
    )

    if style_compact not in build_option.skip_subfamily_list:
        set_font_name(font, font_config.family_name, 16)
        set_font_name(font, style, 17)

    # https://github.com/ftCLI/FoundryTools-CLI/issues/166#issuecomment-2095433585
    if style_with_prefix_space == " Thin":
        font["OS/2"].usWeightClass = 250
    elif style_with_prefix_space == " ExtraLight":
        font["OS/2"].usWeightClass = 275

    freeze(font=font, freeze_config=font_config.feature_freeze)

    font.save(_path)
    font.close()

    run(
        f"ftcli ttf autohint {_path} -out {build_option.output_ttf_hinted}/{font_config.family_name_compact}-AutoHint-{style_compact}.ttf"
    )
    run(f"ftcli converter ttf2otf {_path} -out {build_option.output_otf}")
    run(f"ftcli converter ft2wf {_path} -out {build_option.output_woff2} -f woff2")


def build_nf_by_prebuild_nerd_font(
    font_basename: str, font_config: FontConfig, build_option: BuildOption
) -> TTFont:
    merger = Merger()
    return merger.merge(
        [
            path.join(build_option.ttf_base_dir, font_basename),
            f"{build_option.src_dir}/MapleMono-NF-Base{'-Mono' if font_config.nerd_font['mono'] else ''}.ttf",
        ]
    )


def build_nf_by_font_patcher(
    font_basename: str, font_config: FontConfig, build_option: BuildOption
) -> TTFont:
    """
    full args: https://github.com/ryanoasis/nerd-fonts?tab=readme-ov-file#font-patcher
    """
    _nf_args = [
        font_config.nerd_font["font_forge_bin"],
        "FontPatcher/font-patcher",
        "-l",
        "--careful",
        "--outputdir",
        build_option.output_nf,
    ] + font_config.nerd_font["glyphs"]

    if font_config.nerd_font["mono"]:
        _nf_args += ["--mono"]

    _nf_args += font_config.nerd_font["extra_args"]

    run(_nf_args + [path.join(build_option.ttf_base_dir, font_basename)])
    nf_file_name = "NerdFont"
    if font_config.nerd_font["mono"]:
        nf_file_name += "Mono"
    _path = path.join(
        build_option.output_nf, font_basename.replace("-", f"{nf_file_name}-")
    )
    font = TTFont(_path)
    remove(_path)
    return font


def build_nf(
    f: str,
    get_ttfont: Callable[[str], TTFont],
    font_config: FontConfig,
    build_option: BuildOption,
):
    print(f"generate NerdFont for {f}")
    makedirs(build_option.output_nf, exist_ok=True)
    nf_font = get_ttfont(f)

    # format font name
    style_compact_nf = f.split("-")[-1].split(".")[0]

    style_nf_with_prefix_space, style_nf_in_2, style_nf, _ = parse_font_name(
        style_name_compact=style_compact_nf,
        skip_subfamily_list=build_option.skip_subfamily_list,
    )

    set_font_name(
        nf_font,
        f"{font_config.family_name} NF{style_nf_with_prefix_space}",
        1,
    )
    set_font_name(nf_font, style_nf_in_2, 2)
    set_font_name(
        nf_font,
        f"{font_config.family_name} NF {style_nf}",
        4,
    )
    postscript_name = f"{font_config.family_name_compact}-NF-{style_compact_nf}"
    set_font_name(nf_font, postscript_name, 6)
    set_font_name(
        nf_font,
        get_unique_identifier(
            postscript_name=postscript_name,
            freeze_config=font_config.feature_freeze,
        ),
        3,
    )

    if style_compact_nf not in build_option.skip_subfamily_list:
        set_font_name(nf_font, f"{font_config.family_name} NF", 16)
        set_font_name(nf_font, style_nf, 17)

    _path = path.join(
        build_option.output_nf,
        f"{font_config.family_name_compact}-NF-{style_compact_nf}.ttf",
    )
    nf_font.save(_path)
    nf_font.close()


def build_cn(f: str, font_config: FontConfig, build_option: BuildOption):
    style_compact_cn = f.split("-")[-1].split(".")[0]

    print(f"generate CN font for {f}")

    merger = Merger()
    font = merger.merge(
        [
            path.join(build_option.cn_base_font_dir, f),
            path.join(
                build_option.cn_static_path, f"MapleMonoCN-{style_compact_cn}.ttf"
            ),
        ]
    )

    style_cn_with_prefix_space, style_cn_in_2, style_cn, _ = parse_font_name(
        style_name_compact=style_compact_cn,
        skip_subfamily_list=build_option.skip_subfamily_list,
    )

    set_font_name(
        font,
        f"{font_config.family_name} {build_option.cn_suffix}{style_cn_with_prefix_space}",
        1,
    )
    set_font_name(font, style_cn_in_2, 2)
    set_font_name(
        font,
        f"{font_config.family_name} {build_option.cn_suffix} {style_cn}",
        4,
    )
    postscript_name = f"{font_config.family_name_compact}-{build_option.cn_suffix_compact}-{style_compact_cn}"
    set_font_name(font, postscript_name, 6)
    set_font_name(
        font,
        get_unique_identifier(
            postscript_name=postscript_name,
            freeze_config=font_config.feature_freeze,
            narrow=font_config.cn["narrow"],
        ),
        3,
    )

    if style_compact_cn not in build_option.skip_subfamily_list:
        set_font_name(font, f"{font_config.family_name} {build_option.cn_suffix}", 16)
        set_font_name(font, style_cn, 17)

    font["OS/2"].xAvgCharWidth = 600

    # https://github.com/subframe7536/maple-font/issues/188
    fix_cv98(font)

    freeze(font=font, freeze_config=font_config.feature_freeze)

    if font_config.cn["narrow"]:
        change_char_width(font=font, match_width=1200, target_width=1000)

    # https://github.com/subframe7536/maple-font/issues/239
    # remove_locl(font)

    if font_config.cn["fix_meta_table"]:
        # add code page, Latin / Japanese / Simplify Chinese / Traditional Chinese
        font["OS/2"].ulCodePageRange1 = 1 << 0 | 1 << 17 | 1 << 18 | 1 << 20

        # fix meta table, https://learn.microsoft.com/en-us/typography/opentype/spec/meta
        meta = newTable("meta")
        meta.data = {
            "dlng": "Latn, Hans, Hant, Jpan",
            "slng": "Latn, Hans, Hant, Jpan",
        }
        font["meta"] = meta

    _path = path.join(
        build_option.output_cn,
        f"{font_config.family_name_compact}-{build_option.cn_suffix_compact}-{style_compact_cn}.ttf",
    )

    font.save(_path)
    font.close()


def main():
    check_ftcli()
    font_config = FontConfig()
    font_config.load_external(args=parse_args())
    build_option = BuildOption(font_config)
    build_option.load_cn_dir_and_suffix(
        font_config.cn["with_nerd_font"] and font_config.nerd_font["enable"]
    )

    print("=== [Clean Cache] ===")
    shutil.rmtree(build_option.output_dir, ignore_errors=True)
    shutil.rmtree(build_option.output_woff2, ignore_errors=True)
    makedirs(build_option.output_dir, exist_ok=True)
    makedirs(build_option.output_variable, exist_ok=True)

    start_time = time.time()
    print("=== [Build Start] ===")
    # =========================================================================================
    # ===================================   build basic   =====================================
    # =========================================================================================

    input_files = [
        f"{build_option.src_dir}/MapleMono-Italic[wght]-VF.ttf",
        f"{build_option.src_dir}/MapleMono[wght]-VF.ttf",
    ]
    for input_file in input_files:
        font = TTFont(input_file)

        set_font_name(font, font_config.family_name, 1)
        set_font_name(
            font,
            get_font_name(font, 4).replace("Maple Mono", font_config.family_name),
            4,
        )
        var_postscript_name = get_font_name(font, 6).replace(
            "MapleMono", font_config.family_name_compact
        )
        set_font_name(font, var_postscript_name, 6)
        set_font_name(
            font,
            get_unique_identifier(
                postscript_name=var_postscript_name,
                freeze_config=font_config.feature_freeze,
                suffix="variable",
            ),
            3,
        )
        set_font_name(font, font_config.family_name_compact, 25)

        font.save(
            input_file.replace(
                build_option.src_dir, build_option.output_variable
            ).replace("MapleMono", font_config.family_name_compact)
        )

    run(f"ftcli fix italic-angle {build_option.output_variable}")
    run(f"ftcli fix monospace {build_option.output_variable}")
    run(
        f"ftcli converter vf2i {build_option.output_variable} -out {build_option.output_ttf}"
    )
    run(f"ftcli fix italic-angle {build_option.output_ttf}")
    run(f"ftcli fix monospace {build_option.output_ttf}")
    run(f"ftcli fix strip-names {build_option.output_ttf}")
    run(f"ftcli ttf dehint {build_option.output_ttf}")
    run(f"ftcli ttf fix-contours {build_option.output_ttf}")
    run(f"ftcli ttf remove-overlaps {build_option.output_ttf}")

    _build_mono = partial(
        build_mono, font_config=font_config, build_option=build_option
    )

    with Pool(font_config.pool_size) as p:
        p.map(_build_mono, listdir(build_option.output_ttf))

    # =========================================================================================
    # ====================================   build NF   =======================================
    # =========================================================================================

    if font_config.nerd_font["enable"]:
        get_ttfont_fn = (
            partial(
                build_nf_by_font_patcher,
                font_config=font_config,
                build_option=build_option,
            )
            if font_config.should_use_font_patcher()
            else partial(
                build_nf_by_prebuild_nerd_font,
                font_config=font_config,
                build_option=build_option,
            )
        )
        _build_fn = partial(
            build_nf,
            get_ttfont=get_ttfont_fn,
            font_config=font_config,
            build_option=build_option,
        )
        _version = font_config.nerd_font["version"]
        print(f"patch Nerd Font v{_version}...")

        with Pool(font_config.pool_size) as p:
            p.map(_build_fn, listdir(build_option.output_ttf))

    # =========================================================================================
    # ====================================   build CN   =======================================
    # =========================================================================================

    if font_config.cn["enable"] and path.exists(f"{build_option.src_dir}/cn"):
        if (
            not path.exists(build_option.cn_static_path)
            or font_config.cn["clean_cache"]
        ):
            print("=========================================")
            print("instantiating CN Base font, be patient...")
            print("=========================================")
            run(
                f"ftcli converter vf2i {build_option.src_dir}/cn -out {build_option.cn_static_path}"
            )
            run(f"ftcli ttf fix-contours {build_option.cn_static_path}")
            run(f"ftcli ttf remove-overlaps {build_option.cn_static_path}")
            run(f"ftcli utils del-table -t kern -t GPOS {build_option.cn_static_path}")

        makedirs(build_option.output_cn, exist_ok=True)

        _build_cn_alt = partial(
            build_cn, font_config=font_config, build_option=build_option
        )
        with Pool(font_config.pool_size) as p:
            p.map(_build_cn_alt, listdir(build_option.cn_base_font_dir))

        if font_config.cn["use_hinted"]:
            run(f"ftcli ttf autohint {build_option.output_cn}")

        if font_config.use_cn_both and font_config.release_mode:
            makedirs(build_option.output_cn, exist_ok=True)

            use_nerd_font = not font_config.cn["with_nerd_font"]

            if use_nerd_font and font_config.nerd_font["enable"]:
                build_option.load_cn_dir_and_suffix(use_nerd_font)
                _build_cn_alt = partial(
                    build_cn, font_config=font_config, build_option=build_option
                )
                with Pool(font_config.pool_size) as p:
                    p.map(_build_cn_alt, listdir(build_option.cn_base_font_dir))

                if font_config.cn["use_hinted"]:
                    run(f"ftcli ttf autohint {build_option.output_cn}")

    run(f"ftcli name del-mac-names -r {build_option.output_dir}")

    # write config to output path
    with open(
        path.join(build_option.output_dir, "build-config.json"), "w", encoding="utf-8"
    ) as config_file:
        result = {
            "family_name": font_config.family_name,
            "use_hinted": font_config.use_hinted,
            "feature_freeze": font_config.feature_freeze,
            "nerd_font": font_config.nerd_font,
            "cn": font_config.cn,
        }
        del result["nerd_font"]["font_forge_bin"]
        config_file.write(
            json.dumps(
                result,
                indent=4,
            )
        )

    # =========================================================================================
    # ====================================   release   ========================================
    # =========================================================================================

    def compress_folder(
        source_file_or_dir_path: str, target_parent_dir_path: str
    ) -> str:
        """
        compress folder and return sha1
        """
        source_folder_name = path.basename(source_file_or_dir_path)

        zip_path = path.join(
            target_parent_dir_path,
            f"{font_config.family_name_compact}-{source_folder_name}.zip",
        )

        with ZipFile(
            zip_path, "w", compression=ZIP_DEFLATED, compresslevel=5
        ) as zip_file:
            for root, _, files in walk(source_file_or_dir_path):
                for file in files:
                    file_path = path.join(root, file)
                    zip_file.write(
                        file_path, path.relpath(file_path, source_file_or_dir_path)
                    )
            zip_file.write("OFL.txt", "LICENSE.txt")
            if not source_file_or_dir_path.endswith("Variable"):
                zip_file.write(
                    path.join(build_option.output_dir, "build-config.json"),
                    "config.json",
                )

        zip_file.close()
        sha1 = hashlib.sha1()
        with open(zip_path, "rb") as zip_file:
            while True:
                data = zip_file.read(1024)
                if not data:
                    break
                sha1.update(data)

        return sha1.hexdigest()

    if font_config.release_mode:
        print("=== [Release Mode] ===")

        # archieve fonts
        release_dir = path.join(build_option.output_dir, "release")
        makedirs(release_dir, exist_ok=True)

        hash_map = {}

        # archieve fonts
        for f in listdir(build_option.output_dir):
            if f == "release" or f.endswith(".json"):
                continue
            hash_map[f] = compress_folder(
                source_file_or_dir_path=path.join(build_option.output_dir, f),
                target_parent_dir_path=release_dir,
            )
            print(f"archieve: {f}")

        # write sha1
        with open(
            path.join(release_dir, "sha1.json"), "w", encoding="utf-8"
        ) as hash_file:
            hash_file.write(json.dumps(hash_map, indent=4))

    print(f"=== [Build Success ({time.time() - start_time:.2f} s)] ===")


if __name__ == "__main__":
    main()
