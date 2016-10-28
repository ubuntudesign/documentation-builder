# Core modules
import re
import tempfile
from collections import Mapping
from copy import deepcopy
from glob import glob, iglob
from os import makedirs, path

# Third party modules
import frontmatter
import yaml
from bs4 import BeautifulSoup
from git import Repo
from yaml.scanner import ScannerError
from yaml.parser import ParserError

# Local modules
from .utilities import (
    matching_metadata,
    mergetree,
    relativize,
    replace_link_paths
)


def compile_metadata(metadata_items, context_path):
    metadata = {}

    for dirpath, item in matching_metadata(metadata_items, context_path):
        metadata_tree = deepcopy(item['content'])
        metadata_tree = relativize_paths(
            metadata_tree,
            dirpath,
            context_path
        )
        metadata.update(metadata_tree)

    return metadata


def copy_media(media_path, output_media_path):
    """
    Copy media files from source_media_path to output_media_path
    """

    media_paths_match = path.relpath(
        media_path, output_media_path
    ) == '.'

    if not media_paths_match:
        mergetree(media_path, output_media_path)

        return True


def find_files(source_path, output_path, metadata_items):
    """
    Find all markdown files in the source_path,
    check if they have built versions in the output_path. Check which is newer
    and if the metadata contains any relevant changes.
    Return four lists:
        (new_files, modified_files, unmodified_files, uppercase_files)
    """

    uppercase_files = []
    new_files = []
    modified_files = []
    unmodified_files = []

    for filepath in iglob(
        path.normpath(path.join(source_path, '**/*.md')),
        recursive=True
    ):
        local_filepath = path.relpath(filepath, source_path)
        local_dir = path.normpath(path.dirname(local_filepath))
        filename = path.basename(filepath)
        name = path.splitext(filename)[0]

        output_filepath = path.join(
            output_path,
            path.join(local_dir, name)
        ) + '.html'

        if re.sub(r'\W+', '', name).isupper():
            uppercase_files.append(filepath)
        elif not path.isfile(output_filepath):
            new_files.append(filepath)
        else:
            metadata_modified = 0

            for dirpath, item in matching_metadata(metadata_items, local_dir):
                metadata_modified = max(
                    metadata_modified,
                    item['modified']
                )

            # Check if the file is modified
            modified = max(metadata_modified, path.getmtime(filepath))
            if path.getmtime(output_filepath) < modified:
                modified_files.append(filepath)
            else:
                unmodified_files.append(filepath)

    return (new_files, modified_files, unmodified_files, uppercase_files)


def find_metadata(directory_path):
    """
    Find all metadata.yaml files inside a directory.
    Return them in the format:
    {
        'some/folder': {
            'modified': [mtime],
            'content': [yaml object]
        },
        ...
    }
    """

    metadata_items = {}

    files_match = path.normpath(
        '{root}/**/metadata.yaml'.format(root=directory_path)
    )
    files = glob(files_match, recursive=True)

    if not files:
        raise EnvironmentError('No metadata.yaml files found')

    for filepath in files:
        with open(filepath) as metadata_file:
            filedir = path.normpath(path.dirname(filepath))
            directory = path.relpath(filedir, directory_path)
            metadata_items[directory] = {
                'modified': path.getmtime(filepath),
                'content': yaml.load(metadata_file.read()) or {}
            }

    return metadata_items


def parse_markdown(parser, template, filepath, metadata):
    parser.reset()

    # Try to extract frontmatter metadata
    with open(filepath, encoding="utf-8") as markdown_file:
        file_content = markdown_file.read()

        try:
            file_parts = frontmatter.loads(file_content)
            metadata.update(file_parts.metadata)
            metadata['content'] = parser.convert(file_parts.content)
        except (ScannerError, ParserError):
            """
            If there's a parsererror, it may be because there is no YAML
            frontmatter, so it got confused. Let's just continue.
            """

            metadata['content'] = parser.convert(file_content)

    # Now add on any multimarkdown-format metadata
    if hasattr(parser, 'Meta'):
        # Restructure markdown parser metadata to the same format as we expect
        markdown_meta = parser.Meta

        for name, value in markdown_meta.items():
            if type(value) == list and len(value) == 1:
                markdown_meta[name] = value[0]

        metadata.update(markdown_meta)

    if metadata.get('table_of_contents'):
        toc_soup = BeautifulSoup(parser.toc, 'html.parser')

        # Get title list item (<h1>)
        nav_item_strings = []

        # Only get <h2> items, to avoid getting crazy
        for item in toc_soup.select('.toc > ul > li > ul > li'):
            for child in item('ul'):
                child.extract()

            item['class'] = 'p-toc__item'

            nav_item_strings.append(str(item))

        metadata['toc_items'] = "\n".join(nav_item_strings)

    return template.render(metadata)


def prepare_branches(base_directory, output_base, versions=False):
    """
    If build_version_branches is true, look for a "versions" file in the
    base_directory and then pull each version branch.
    Otherwise, just return the base directory.
    """

    branch_paths = []

    if not path.isdir(base_directory):
        raise FileNotFoundError(
            'Base directory not found: {}'.format(base_directory)
        )

    if versions:
        with open(path.join(base_directory, 'versions')) as versions_file:
            lines = versions_file.read().splitlines()
            version_branches = list(filter(None, lines))

        for branch in version_branches:
            branch_output = path.join(output_base, branch)
            branch_dir = tempfile.mkdtemp()
            Repo.clone_from(base_directory, branch_dir, branch=branch)
            branch_paths.append(
                (branch_dir, branch_output)
            )
    else:
        branch_paths.append(
            (base_directory, output_base)
        )

    return branch_paths


def relativize_paths(item, original_base_path, new_base_path):
    """
    Recursively search a dictionary for items that look like local markdown
    locations, and replace them to be relative to local_dirpath instead
    """

    internal_link_match = r'^[^ "\']+.md(#|\?|$)'

    original_base_path = original_base_path.strip('/')
    new_base_path = new_base_path.strip('/')

    if isinstance(item, Mapping):
        for key, child in item.items():
            item[key] = relativize_paths(
                child,
                original_base_path,
                new_base_path
            )
    elif isinstance(item, list):
        for index, child in enumerate(item):
            item[index] = relativize_paths(
                child,
                original_base_path,
                new_base_path
            )
    elif isinstance(item, str) and re.match(internal_link_match, item):
        item = relativize(
            item,
            original_base_path,
            new_base_path
        )

    return item


def replace_internal_links(html, extensions=True):
    internal_link_match = re.compile(
        r'(?:(?<=src=["\'])|(?<=href=["\']))'
        r'((?:[^ "\'/]|(?<![/"\'])/)+)\.md\b'
    )

    # Replace internal document links
    if extensions:
        html = re.sub(
            internal_link_match,
            r'\1.html',
            html
        )
    else:
        html = re.sub(
            internal_link_match,
            r'\1',
            html
        )

    return html


def replace_media_links(
    html,
    old_path,
    new_path,
    context_directory='.'
):
    """
    Replace links to media with the new media location.
    Do this intelligently relative to the current directory of the file.
    """

    if old_path:
        if not path.isabs(old_path):
            old_path = path.relpath(old_path, context_directory)
        if not path.isabs(new_path):
            new_path = path.relpath(new_path, context_directory)

        html = replace_link_paths(html, old_path, new_path)

    return html


def write_html(html, output_filepath):
    """
    Write HTML content to an HTML file
    """

    # Check the extension is right
    output_filepath = path.splitext(output_filepath)[0] + '.html'
    output_dir = path.dirname(output_filepath)

    makedirs(output_dir, exist_ok=True)

    with open(
        output_filepath, mode="w", encoding="utf-8"
    ) as output_file:
        output_file.write(html)

    return output_filepath
