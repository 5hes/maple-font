def freeze_feature(font, moving_rules, config):
    # check feature list
    feature_record = font["GSUB"].table.FeatureList.FeatureRecord
    feature_dict = {
        feature.FeatureTag: feature.Feature
        for feature in feature_record
        if feature.FeatureTag != "calt"
    }

    calt_features = [
        feature.Feature for feature in feature_record if feature.FeatureTag == "calt"
    ]

    # Process features
    for tag, status in config.items():
        target_feature = feature_dict.get(tag)
        if not target_feature or status == "ignore":
            continue

        if status == "disable":
            target_feature.LookupListIndex = []
            continue

        if tag in moving_rules:
            # Enable by moving rules into "calt"
            for calt_feat in calt_features:
                calt_feat.LookupListIndex.extend(target_feature.LookupListIndex)
        else:
            # Enable by replacing data in glyf and hmtx tables
            glyph_dict = font["glyf"].glyphs
            hmtx_dict = font["hmtx"].metrics
            for index in target_feature.LookupListIndex:
                lookup = font["GSUB"].table.LookupList.Lookup[index]
                for old_key, new_key in lookup.SubTable[0].mapping.items():
                    if (
                        old_key in glyph_dict
                        and old_key in hmtx_dict
                        and new_key in glyph_dict
                        and new_key in hmtx_dict
                    ):
                        glyph_dict[old_key] = glyph_dict[new_key]
                        hmtx_dict[old_key] = hmtx_dict[new_key]
                    else:
                        print(f"{old_key} or {new_key} does not exist")
                        return