# Values in the dataset's manifests that belong to a deployment, not to the
# template: where each object lives (bucket, Neon project, table bucket,
# region). check.sh rewrites them to <deployment> on both sides before
# comparing -- in packages/dataset/tether.toml and .tether/objects/*.toml only
# -- so a copy can point the dataset at its own stores while object keys,
# kinds and policies still track the template. Whatever writes the copy's
# real locators (a script reading its infrastructure's outputs, or `tether
# add` by hand) must produce values these patterns match; extend them for a
# locator of another shape.
#
# The other way out is a dataset in a repository of its own, brought in as a
# submodule (the README, "Where the dataset lives"): then the shared packages
# carry no locators at all, at the price of a second repo per deployment whose
# manifests nothing compares with the template's.
s#"s3://[^/"]+/#"s3://<deployment>/#g
s#^(project_id|region) = "[^"]*"#\1 = "<deployment>"#
s#warehouse = "[^"]*"#warehouse = "<deployment>"#g
