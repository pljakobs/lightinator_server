import json
import os
import sys
import re
import shutil
import base64
import argparse
import hashlib
from pathlib import Path
from urllib.parse import urlparse

# Note: webapp bundle sizes are derived by version_api.py (per-file "size" and a
# top-level "total_size"), preferring an explicit "size" in version.json and
# falling back to stat'ing the deployed file. Storing sizes here at add-webapp
# time remains an optional future optimisation.

def update_latest_symlink(data, base_dir='download/firmware'):
    """
    For each branch (develop, testing, main), create or update a symlink (or copy) in download/latest-<branch>/app_merged.bin
    pointing to the latest single_image/app_merged.bin for that branch.
    """
    branches = ['develop', 'testing', 'main']
    for branch in branches:
        # Find all firmware entries for this branch and single_image builds
        candidates = [e for e in data['firmware'] if e['branch'] == branch and 'single_image' in e['files']['rom']['url']]
        if not candidates:
            continue
        # Sort by version (descending)
        candidates.sort(key=lambda e: extract_complete_version_number(e['fw_version']), reverse=True)
        latest = candidates[0]
        url = latest['files']['rom']['url']
        # Extract the local path to app_merged.bin
        parsed = urlparse(url)
        rel_path = parsed.path.lstrip('/')
        if rel_path.startswith(base_dir.rstrip('/') + '/'):
            src_path = rel_path
        elif rel_path.startswith('download/'):
            src_path = rel_path
        else:
            src_path = os.path.join(base_dir, rel_path)
        # Target symlink directory
        link_dir = os.path.join(base_dir, f'latest-{branch}')
        os.makedirs(link_dir, exist_ok=True)
        link_path = os.path.join(link_dir, 'app_merged.bin')
        # Remove old symlink or file if exists
        if os.path.islink(link_path) or os.path.exists(link_path):
            os.remove(link_path)
        try:
            os.symlink(os.path.abspath(src_path), link_path)
            print(f"Created symlink: {link_path} -> {src_path}")
        except Exception as e:
            print(f"Failed to create symlink for {branch}: {e}")


def load_json(file_path_or_url):
    # Check if the input is a URL
    if file_path_or_url.startswith('http://') or file_path_or_url.startswith('https://'):
        try:
            response = requests.get(file_path_or_url, timeout=10)
            response.raise_for_status()  # Raise an exception for HTTP errors
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching from URL: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from URL: {e}")
            return None
    
    # Handle local file
    if not os.path.exists(file_path_or_url):
        return None
    with open(file_path_or_url, 'r') as file:
        try:
            return json.load(file)
        except json.JSONDecodeError:
            return None

def save_json(data, file_path):
    with open(file_path, 'w') as file:
        json.dump(data, file, indent=4)

def prime_json(file_path):
    data = {
        "rom": {
            "fw_version": "pj-1-656-g3119",
            "url": "http://lightinator.de/download/esp8266/develop/release/rom0.bin"
        },
        "spiffs": {
            "webapp_version": "0.3.3",
            "url": "http://rgbww.dronezone.de/testing/spiff_rom.bin"
        },
        "firmware": [],
        "history": []  # Add the history array to the base structure
    }
    save_json(data, file_path)
    return data

def extract_build_number(version_string):
    """Extract prerelease/build number from old and new version strings."""
    return parse_version_components(version_string)[3]

def get_version_limit(branch):
    """Get maximum number of versions to keep based on branch"""
    if branch == 'stable':
        return 4
    elif branch == 'testing':
        return 4
    else:  # develop or any other branch
        return 10

def extract_directory_from_url(url):
    """Extract the directory path from a URL"""
    parsed_url = urlparse(url)
    path = parsed_url.path
    # Remove the filename from the path
    directory = os.path.dirname(path)
    return directory

def parse_version_components(version_string):
    """Parse old and new firmware version formats into sortable components."""
    normalized = version_string.strip().lstrip('Vv')
    if not normalized:
        return (0, 0, 0, 0)

    segments = normalized.split('-')
    base_parts = segments[0].split('.')

    try:
        major = int(base_parts[0]) if len(base_parts) > 0 else 0
        minor = int(base_parts[1]) if len(base_parts) > 1 else 0
        patch = int(base_parts[2]) if len(base_parts) > 2 else 0
    except ValueError:
        return (0, 0, 0, 0)

    prerelease = 0
    if len(segments) > 1 and segments[1].isdigit():
        prerelease = int(segments[1])

    return (major, minor, patch, prerelease)

def add_or_update_entry(data, soc, type_, branch, fw_version, url, comment=''):
    # Make sure history array exists
    if "history" not in data:
        data["history"] = []
    
    # Debug: Print the entry we're trying to add
    print(f"Adding/updating: {soc}/{type_}/{branch}: {fw_version}")
    print(f"Current firmware entries: {len(data['firmware'])}")
    
    # Normalize inputs to prevent case/whitespace issues
    soc = soc.strip().lower()
    type_ = type_.strip().lower()
    branch = branch.strip().lower()
    comment = comment or ''
    
    # First check if entry already exists and update it if it does
    entry_exists = False
    for i, entry in enumerate(data['firmware']):
        # Normalize existing entry data for comparison
        entry_soc = entry['soc'].strip().lower()
        entry_type = entry['type'].strip().lower()
        entry_branch = entry['branch'].strip().lower()
        
        # Debug: Print comparison details
        print(f"Comparing with: {entry_soc}/{entry_type}/{entry_branch}")
        
        if (entry_soc == soc and
            entry_type == type_ and
            entry_branch == branch):
            # Found matching entry
            print(f"Match found: {entry['soc']}/{entry['type']}/{entry['branch']}")
            
            # Keep original case for the entry we're updating
            original_soc = entry['soc']
            original_type = entry['type']
            original_branch = entry['branch']
            existing_comment = entry.get('comment', '')
            updated_comment = comment if comment else existing_comment
            
            if entry['fw_version'] == fw_version:
                # Update existing entry with same version - no history change
                print(f"Same version, just updating URL")
                data['firmware'][i] = {
                    "soc": original_soc,
                    "type": original_type,
                    "branch": original_branch,
                    "fw_version": fw_version,
                    "version":fw_version,
                    "comment": updated_comment,
                    # Add version key for backward compatibility
                    "files": {
                        "rom": {
                            "url": url
                        }
                    }
                }
            else:
                # Same combination but different version - move to history
                print(f"Moving {original_soc}/{original_type}/{original_branch}: {entry['fw_version']} to history")
                # Create a copy to avoid reference is=sues
                history_entry = dict(entry)
                data['history'].append(history_entry)
                
                # Replace with new version
                data['firmware'][i] = {
                    "soc": original_soc,
                    "type": original_type,
                    "branch": original_branch,
                    "fw_version": fw_version,
                    "version":fw_version,
                    "comment": comment,
                    "files": {
                        "rom": {
                            "url": url
                        }
                    }
                }
            entry_exists = True
            break
    
    # If no matching soc/type/branch entry exists, add it
    if not entry_exists:
        print(f"No existing entry found, adding new one")
        # Add the new entry
        new_entry = {
            "soc": soc,
            "type": type_,
            "branch": branch,
            "fw_version": fw_version,
            "version":fw_version,
            "comment": comment,
            "files": {
                "rom": {
                    "url": url
                }
            }
        }
        data['firmware'].append(new_entry)
    
    # Update the top-level rom entry for backward compatibility
    if soc == 'esp8266' and type_ == 'release' and branch == 'testing':
        data['rom'] = {
            "fw_version": fw_version,
            "url": url
        }
    
    # Debug: Final state
    print(f"Final firmware entries: {len(data['firmware'])}")
    print(f"Final history entries: {len(data['history'])}")
    
    return data

def delete_entry(data, soc, type_, branch, fw_version=None, delete_files=False):
    entries_to_delete = []
    
    # Find entries to delete from firmware array
    if fw_version:
        # Delete specific version
        entries_to_delete = [entry for entry in data['firmware'] 
                            if (entry['soc'] == soc and 
                                entry['type'] == type_ and 
                                entry['branch'] == branch and 
                                entry['fw_version'] == fw_version)]
        data['firmware'] = [entry for entry in data['firmware'] 
                           if not (entry['soc'] == soc and 
                                  entry['type'] == type_ and 
                                  entry['branch'] == branch and 
                                  entry['fw_version'] == fw_version)]
                                  
        # Also remove from history if present
        history_entries_to_delete = [entry for entry in data.get('history', []) 
                                    if (entry['soc'] == soc and 
                                        entry['type'] == type_ and 
                                        entry['branch'] == branch and 
                                        entry['fw_version'] == fw_version)]
        if 'history' in data:
            data['history'] = [entry for entry in data['history'] 
                              if not (entry['soc'] == soc and 
                                      entry['type'] == type_ and 
                                      entry['branch'] == branch and 
                                      entry['fw_version'] == fw_version)]
        
        entries_to_delete.extend(history_entries_to_delete)
    else:
        # Delete all versions for this combination
        entries_to_delete = [entry for entry in data['firmware'] 
                            if (entry['soc'] == soc and 
                                entry['type'] == type_ and 
                                entry['branch'] == branch)]
        data['firmware'] = [entry for entry in data['firmware'] 
                           if not (entry['soc'] == soc and 
                                  entry['type'] == type_ and 
                                  entry['branch'] == branch)]
                                  
        # Also remove from history if present
        if 'history' in data:
            history_entries_to_delete = [entry for entry in data['history'] 
                                        if (entry['soc'] == soc and 
                                            entry['type'] == type_ and 
                                            entry['branch'] == branch)]
            data['history'] = [entry for entry in data['history'] 
                              if not (entry['soc'] == soc and 
                                      entry['type'] == type_ and 
                                      entry['branch'] == branch)]
            
# ...existing code...

# Place this function after all imports, before main()
            entries_to_delete.extend(history_entries_to_delete)
    
    # Delete corresponding directories if requested
    if delete_files:
        for entry in entries_to_delete:
            try:
                url = entry['files']['rom']['url']
                directory = extract_directory_from_url(url)
                local_dir = os.path.join('.', directory.lstrip('/'))
                
                # Get version directory (one level up from SOC/type)
                version_dir = os.path.dirname(os.path.dirname(local_dir))
                
                print(f"Deleting directory: {version_dir}")
                if os.path.exists(version_dir):
                    shutil.rmtree(version_dir)
                else:
                    print(f"Directory not found: {version_dir}")
            except Exception as e:
                print(f"Error deleting directory: {str(e)}")
    
    return data

def is_url_accessible(url):
    """Check if a URL is accessible (returns 200 status code)"""
    try:
        response = requests.head(url, timeout=5)
        if response.status_code == 405:  # Method not allowed, try GET instead
            response = requests.get(url, timeout=5, stream=True)
            response.close()  # Don't download the whole file
        return response.status_code == 200
    except requests.RequestException:
        return False

def list_entries(data, soc=None, type_=None, branch=None, include_history=False, check_urls=False):
    # Get entries from firmware array
    filtered_entries = data['firmware']
    
    if soc:
        filtered_entries = [e for e in filtered_entries if e['soc'] == soc]
    if type_:
        filtered_entries = [e for e in filtered_entries if e['type'] == type_]
    if branch:
        filtered_entries = [e for e in filtered_entries if e['branch'] == branch]
    
    # Include history entries if requested
    if include_history and 'history' in data:
        history_entries = data['history']
        
        if soc:
            history_entries = [e for e in history_entries if e['soc'] == soc]
        if type_:
            history_entries = [e for e in history_entries if e['type'] == type_]
        if branch:
            history_entries = [e for e in history_entries if e['branch'] == branch]
            
        # Combine entries but mark history entries
        for entry in history_entries:
            entry['is_history'] = True
            
        for entry in filtered_entries:
            entry['is_history'] = False
            
        filtered_entries.extend(history_entries)
    
    # Check URL accessibility if requested
    if check_urls:
        for entry in filtered_entries:
            try:
                url = entry['files']['rom']['url']
                entry['url_accessible'] = is_url_accessible(url)
            except KeyError:
                entry['url_accessible'] = False
    
    return filtered_entries

def extract_complete_version_number(version_string):
    """Extract a sortable number from old and new firmware version strings."""
    major, minor, patch, prerelease = parse_version_components(version_string)
    return major * 1000000000 + minor * 1000000 + patch * 1000 + prerelease

def cull_history(data, dry_run=False):
    """Limit the number of historical versions based on branch limits"""
    if "history" not in data or not data["history"]:
        print("No history entries to cull")
        return data
    
    # Group entries by soc+type+branch
    grouped_entries = {}
    for entry in data["history"]:
        key = (entry["soc"], entry["type"], entry["branch"])
        if key not in grouped_entries:
            grouped_entries[key] = []
        grouped_entries[key].append(entry)
    
    # Print summary of entries by type before culling
    print("\nHistory entries before culling:")
    for (soc, type_, branch), entries in sorted(grouped_entries.items()):
        print(f"  {soc}/{type_}/{branch}: {len(entries)} entries")
    print(f"  Total: {len(data['history'])} entries")
    
    entries_to_remove = []
# ...existing code...

# Place this function after all imports, before main()
    
    # Process each group
    for (soc, type_, branch), entries in grouped_entries.items():
        # Sort by version (descending)
        sorted_entries = sorted(entries, 
                               key=lambda e: extract_complete_version_number(e["fw_version"]), 
                               reverse=True)
        
        # Get limit for this branch
        limit = get_version_limit(branch)
        
        # Keep only the top N entries
        if len(sorted_entries) > limit:
            entries_to_keep = sorted_entries[:limit]
            to_remove = sorted_entries[limit:]
            
            print(f"For {soc}/{type_}/{branch}: keeping {limit} entries, removing {len(to_remove)} entries")
            
            # Add entries to remove list
            entries_to_remove.extend(to_remove)
            
            if dry_run:
                for entry in to_remove:
                    print(f"Would remove: {entry['soc']}/{entry['type']}/{entry['branch']}: {entry['fw_version']}")
    
    if not entries_to_remove:
        print("No entries need to be culled")
        return data
    
    if not dry_run:
        # Remove entries from history
        for entry in entries_to_remove:
            # Delete files if they exist
            try:
                url = entry['files']['rom']['url']
                directory = extract_directory_from_url(url)
                local_dir = os.path.join('.', directory.lstrip('/'))
                
                # Get version directory (one level up from SOC/type)
                version_dir = os.path.dirname(os.path.dirname(local_dir))
                
                print(f"Deleting directory: {version_dir}")
                if os.path.exists(version_dir):
                    shutil.rmtree(version_dir)
                else:
                    print(f"Directory not found: {version_dir}")
            except Exception as e:
                print(f"Error deleting directory: {str(e)}")
        
        # Update history array to exclude removed entries
        data["history"] = [entry for entry in data["history"] 
                         if entry not in entries_to_remove]
    
    # Print summary of entries by type after culling
    if not dry_run or entries_to_remove:
        # Re-group entries after culling
        new_grouped_entries = {}
        for entry in data["history"]:
            key = (entry["soc"], entry["type"], entry["branch"])
            if key not in new_grouped_entries:
                new_grouped_entries[key] = []
            new_grouped_entries[key].append(entry)
        
        print("\nHistory entries after culling:")
        for (soc, type_, branch), entries in sorted(new_grouped_entries.items()):
            print(f"  {soc}/{type_}/{branch}: {len(entries)} entries")
        print(f"  Total: {len(data['history'])} entries")
    
    return data

def add_or_update_webapp_entry(data, branch, version, files=None, comment='', webapp_type='debug', min_firmware=''):
    """Add or update a webapp entry in version.json."""
    if "webapp" not in data:
        data["webapp"] = []
    if "webapp_history" not in data:
        data["webapp_history"] = []
    files = files or []
    branch = branch.strip().lower()
    webapp_type = webapp_type.strip().lower() if webapp_type else 'debug'
    if webapp_type not in ('debug', 'release'):
        print(f"Invalid webapp type '{webapp_type}', defaulting to 'debug'")
        webapp_type = 'debug'
    min_firmware = min_firmware.strip()
    comment = comment or ''

    for i, entry in enumerate(data["webapp"]):
        if entry["branch"].strip().lower() == branch:
            if entry["version"] == version:
                data["webapp"][i] = {
                    "version": version,
                    "type": webapp_type,
                    "branch": entry["branch"],
                    "min_firmware": min_firmware,
                    "files": files,
                    "comment": comment if comment else entry.get("comment", "")
                }
                print(f"Updated webapp entry for {branch} version {version} (same version, metadata/files updated)")
            else:
                print(f"Moving webapp {branch}: {entry['version']} to history")
                data["webapp_history"].append(dict(entry))
                data["webapp"][i] = {
                    "version": version,
                    "type": webapp_type,
                    "branch": branch,
                    "min_firmware": min_firmware,
                    "files": files,
                    "comment": comment
                }
                print(f"Added new webapp entry for {branch} version {version}")
            return data

    data["webapp"].append({
        "version": version,
        "type": webapp_type,
        "branch": branch,
        "min_firmware": min_firmware,
        "files": files,
        "comment": comment
    })
    print(f"Added new webapp entry for {branch} version {version}")
    return data


def list_webapp_entries(data, branch=None):
    """List webapp entries, optionally filtered by branch."""
    entries = data.get("webapp", [])
    if branch:
        branch = branch.strip().lower()
        entries = [e for e in entries if e["branch"].strip().lower() == branch]
    return entries


def legacy_json_main():
    global requests
    import requests
    if len(sys.argv) < 3:
        print("Usage: script.py <json_file_or_url> [add|delete|list|cull] [arguments]")
        return

    file_path_or_url = sys.argv[1]
    action = sys.argv[2]

    # Load the data
    data = load_json(file_path_or_url)
    if data is None:
        if not (file_path_or_url.startswith('http://') or file_path_or_url.startswith('https://')):
            # Only create a new file if it's a local path
            pass
        return

    if action == 'add':
        if len(sys.argv) < 8:
            print("Usage: script.py <json_file> add <soc> <type> <branch> <fw_version> <url> [--comment-b64 <base64>]")
            return
        soc = sys.argv[3]
        type_ = sys.argv[4]
        branch = sys.argv[5]
        fw_version = sys.argv[6]
        url = sys.argv[7]
        comment = ''
        args = sys.argv[8:]
        idx = 0
        while idx < len(args):
            if args[idx] == '--comment-b64' and idx + 1 < len(args):
                try:
                    comment = base64.b64decode(args[idx + 1]).decode('utf-8')
                except Exception as exc:
                    print(f"Error decoding --comment-b64: {exc}")
                    return
                idx += 2
            else:
                idx += 1
        data = add_or_update_entry(data, soc, type_, branch, fw_version, url, comment)
        save_json(data, file_path_or_url)
        update_latest_symlink(data)
        print(f"Entry added/updated for {soc}/{type_}/{branch} version {fw_version}.")
        return

    if action == 'delete':
        if len(sys.argv) < 6:
            print("Usage: script.py <json_file> delete <soc> <type> <branch> [fw_version] [--keep-files]")
            return
        soc = sys.argv[3]
        type_ = sys.argv[4]
        branch = sys.argv[5]
        fw_version = None
        delete_files = True
        for arg in sys.argv[6:]:
            if arg == '--keep-files':
                delete_files = False
            elif not fw_version:
                fw_version = arg
        data = delete_entry(data, soc, type_, branch, fw_version, delete_files)
        save_json(data, file_path_or_url)
        update_latest_symlink(data)
        print(f"Entry deleted for {soc}/{type_}/{branch} version {fw_version}.")
        return

    if action == 'cull':
        dry_run = False
        for arg in sys.argv[3:]:
            if arg == '--dry-run':
                dry_run = True
        print(f"Culling history entries (dry run: {dry_run})")
        data = cull_history(data, dry_run)
        if not dry_run:
            save_json(data, file_path_or_url)
            update_latest_symlink(data)
        return

    if action == 'list':
        soc = None
        type_ = None
        branch = None
        include_history = False
        check_urls = False
        # Parse arguments
        for arg in sys.argv[3:]:
            if arg == '--history':
                include_history = True
            elif arg == '--check-urls':
                check_urls = True
            elif soc is None:
                soc = arg
            elif type_ is None:
                type_ = arg
            elif branch is None:
                branch = arg
        entries = list_entries(data, soc, type_, branch, include_history, check_urls)
        print(f"Found {len(entries)} entries:")
        for entry in sorted(entries, key=lambda e: (e['branch'], e['soc'], e['type'], extract_build_number(e['fw_version']), e.get('is_history', False)), reverse=True):
            history_marker = "[HISTORY] " if entry.get('is_history', False) else ""
            url_status = " [✓]" if check_urls and entry.get('url_accessible', False) else " [✗]" if check_urls else ""
            print(f"{history_marker}{entry['soc']}/{entry['type']}/{entry['branch']}: {entry['fw_version']}{url_status} - {entry['files']['rom']['url']}")
        return

    if action == 'add-webapp':
        if len(sys.argv) < 5:
            print("Usage: script.py <json_file> add-webapp <branch> <version> [--files-json-b64 <base64>] [--comment-b64 <base64>] [--type <debug|release>] [--min-firmware <semver>]")
            return
        branch = sys.argv[3]
        version = sys.argv[4]
        files = []
        comment = ''
        webapp_type = 'debug'
        min_firmware = ''
        args = sys.argv[5:]
        idx = 0
        while idx < len(args):
            if args[idx] == '--files-json-b64' and idx + 1 < len(args):
                try:
                    files = json.loads(base64.b64decode(args[idx + 1]).decode('utf-8'))
                except Exception as exc:
                    print(f"Error decoding --files-json-b64: {exc}")
                    return
                idx += 2
            elif args[idx] == '--comment-b64' and idx + 1 < len(args):
                try:
                    comment = base64.b64decode(args[idx + 1]).decode('utf-8')
                except Exception as exc:
                    print(f"Error decoding --comment-b64: {exc}")
                    return
                idx += 2
            elif args[idx] == '--type' and idx + 1 < len(args):
                webapp_type = args[idx + 1]
                idx += 2
            elif args[idx] == '--min-firmware' and idx + 1 < len(args):
                min_firmware = args[idx + 1]
                idx += 2
            else:
                idx += 1
        data = add_or_update_webapp_entry(data, branch, version, files, comment, webapp_type, min_firmware)
        save_json(data, file_path_or_url)
        print(f"Webapp entry added/updated for {branch} version {version}.")
        return

    if action == 'list-webapp':
        branch = sys.argv[3] if len(sys.argv) > 3 else None
        entries = list_webapp_entries(data, branch)
        print(f"Found {len(entries)} webapp entries:")
        for entry in sorted(entries, key=lambda e: e['branch']):
            files_count = len(entry.get("files", []))
            entry_type = entry.get("type", "unknown")
            min_fw = entry.get("min_firmware", "")
            min_fw_text = f" min_firmware={min_fw}" if min_fw else ""
            print(f"  {entry['branch']}: {entry['version']} type={entry_type}{min_fw_text} ({files_count} files)")
        return

    print("Invalid action. Use add, delete, list, cull, add-webapp, or list-webapp.")

def database_main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments == ["--publisher-contract"]:
        print("lightinator-sqlite-v1")
        return
    import version_api as api

    parser = argparse.ArgumentParser(description="Publish rsynced assets directly to SQLite; JSON is a read-only projection")
    parser.add_argument("catalogue", help="Legacy catalogue argument (never read or written)")
    parser.add_argument("--db", default=os.getenv("VERSION_DB_PATH", str(Path(__file__).with_name("firmware.db"))))
    parser.add_argument("--init-db", action="store_true", help="Explicitly initialize a missing database")
    parser.add_argument("--asset-root", type=Path, default=os.getenv("VERSION_ASSET_ROOT") or os.getenv("VERSION_LEGACY_ASSET_ROOT"))
    commands = parser.add_subparsers(dest="action", required=True)
    firmware = commands.add_parser("add")
    for name in ("soc", "type", "branch", "version", "url"):
        firmware.add_argument(name)
    firmware.add_argument("--comment-b64", default="")
    firmware.add_argument("--source-sha", default=None)
    firmware.add_argument("--rgbwwled-version", default=None)
    firmware.add_argument("--upgrade-from-lines", nargs="*", default=None)
    webapp = commands.add_parser("add-webapp")
    webapp.add_argument("branch")
    webapp.add_argument("version")
    webapp.add_argument("--files-json-b64", default="")
    webapp.add_argument("--comment-b64", default="")
    webapp.add_argument("--type", choices=("debug", "release"), default="debug")
    for name in ("min-firmware", "max-firmware", "basepath"):
        webapp.add_argument(f"--{name}", default=None)
    for action in ("list", "delete"):
        command = commands.add_parser(action)
        for name in ("soc", "type", "branch"):
            command.add_argument(name, nargs="?" if action == "list" else None)
        if action == "delete":
            command.add_argument("version", nargs="?")
            command.add_argument("--keep-files", action="store_true")
        else:
            command.add_argument("--history", action="store_true")
            command.add_argument("--check-urls", action="store_true")
    listing = commands.add_parser("list-webapp")
    listing.add_argument("branch", nargs="?")
    cull = commands.add_parser("cull")
    cull.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(arguments)
    database = Path(args.db)
    if not database.is_absolute():
        database = Path(__file__).parent / database
    if not database.is_file() and not args.init_db:
        raise ValueError("Database does not exist; deploy the server first or explicitly use --init-db")
    api.DB_PATH = str(database.resolve())
    api.init_db()
    root = Path(args.asset_root or Path(args.catalogue).resolve().parent).resolve()

    def local_asset(url):
        from urllib.parse import unquote
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            raise ValueError("Asset URL must use HTTP or HTTPS")
        asset = (root / unquote(parsed.path).lstrip("/")).resolve()
        if not asset.is_relative_to(root):
            raise ValueError("Asset URL escapes the configured webroot")
        return asset

    def comment():
        return base64.b64decode(args.comment_b64, validate=True).decode("utf-8") if args.comment_b64 else ""

    if args.action == "add":
        soc, build_type, branch = args.soc.lower(), args.type.lower(), args.branch.lower()
        identity = api.release_identity(args.version, branch)
        if identity is None or build_type not in {"debug", "release"}:
            raise ValueError("Invalid firmware line, stage or build type")
        with api.get_db_connection() as connection:
            product = connection.execute("SELECT target_policy_json FROM ota_products WHERE product_id = 'controller' AND enabled = 1").fetchone()
        policy = json.loads(product["target_policy_json"]) if product else {}
        if soc not in policy.get(identity["release_line"], []):
            raise ValueError("SoC is not supported on the selected release line")
        expected = {"5.0": "0.10.1", "5.1": "0.11.0"}.get(identity["release_line"])
        if args.rgbwwled_version and expected and args.rgbwwled_version != expected:
            raise ValueError("RGBWWLed version does not match release line")
        if args.upgrade_from_lines and any(not re.fullmatch(r"\d+\.\d+", line) for line in args.upgrade_from_lines):
            raise ValueError("Invalid qualified source release line")
        asset = local_asset(args.url)
        if not asset.is_file() or asset.name == "app-merged.bin" or "single_image" in asset.parts:
            raise ValueError("A deployed OTA application binary is required, not a merged flash image")
        payload = asset.read_bytes()
        if not payload:
            raise ValueError("Firmware binary is empty")
        checksum = hashlib.sha256(payload).hexdigest()
        text = comment()
        with api.get_db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM firmware_builds WHERE soc = ? AND type = ? AND branch = ? AND version = ?",
                                          (soc, build_type, branch, args.version)).fetchone()
            if existing and (existing["url"] != args.url or (existing["sha256"] and existing["sha256"] != checksum)):
                raise ValueError("Published artifact is immutable; use a new version")
            approved = json.dumps(args.upgrade_from_lines) if args.upgrade_from_lines is not None else (existing["upgrade_from_lines"] if existing else None)
            source_sha = args.source_sha if args.source_sha is not None else (existing["source_sha"] if existing else None)
            component = args.rgbwwled_version if args.rgbwwled_version is not None else (existing["rgbwwled_version"] if existing else None)
            connection.execute("""INSERT INTO firmware_builds
                (soc, type, branch, version, file_path, url, comment, release_line, stage, sha256, size, source_sha, rgbwwled_version, upgrade_from_lines)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(soc, branch, type, version) DO UPDATE SET
                    file_path=excluded.file_path, comment=excluded.comment,
                    release_line=excluded.release_line, stage=excluded.stage,
                    sha256=excluded.sha256, size=excluded.size, source_sha=excluded.source_sha,
                    rgbwwled_version=excluded.rgbwwled_version, upgrade_from_lines=excluded.upgrade_from_lines""",
                (soc, build_type, branch, args.version, str(asset), args.url, text or (existing["comment"] if existing else ""),
                 identity["release_line"], identity["stage"], checksum, len(payload), source_sha, component, approved))
        print(f"Published firmware to SQLite: {soc}/{identity['source_ref']}/{build_type} {args.version}")
        return

    if args.action == "add-webapp":
        if not all(api._safe_segment(value) for value in args.branch.split("/")) or not api._safe_segment(args.version):
            raise ValueError("Invalid webapp path")
        branch = args.branch.lower()
        basepath = args.basepath or f"{api.DOWNLOAD_BASE_URL}/download/webui/{branch}/{args.version}/"
        directory = local_asset(basepath)
        if not any((directory / name).is_file() for name in ("index.html", "index.html.gz")):
            raise ValueError("Deployed webapp has no root entry point")
        manifest = api.webapp_manifest({"file_path": str(directory), "version": args.version, "branch": branch, "basepath": basepath})
        if args.files_json_b64:
            expected_files = json.loads(base64.b64decode(args.files_json_b64, validate=True).decode("utf-8"))
            actual_files = {entry["filename"]: entry for entry in manifest["files"]}
            if not isinstance(expected_files, list):
                raise ValueError("File manifest must be an array")
            for entry in expected_files:
                name = (entry.get("filename") or entry.get("path")) if isinstance(entry, dict) else None
                if name not in actual_files or entry.get("md5") != actual_files[name]["md5"]:
                    raise ValueError("Deployed asset does not match the CI file manifest")
        digest_entries = [(entry["filename"], hashlib.sha256((directory / entry["filename"]).read_bytes()).hexdigest()) for entry in manifest["files"]]
        checksum = hashlib.sha256(api._canonical_json(digest_entries)).hexdigest()
        text = comment()
        with api.get_db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM webapp_builds WHERE branch = ? AND version = ?", (branch, args.version)).fetchone()
            if existing and ((existing["sha256"] and existing["sha256"] != checksum) or existing["basepath"] != basepath):
                raise ValueError("Published webapp is immutable; use a new version")
            values = {}
            for key in ("min_firmware", "max_firmware", "api_min", "api_max", "required_capabilities"):
                value = getattr(args, key, None)
                values[key] = value if value is not None else ((existing[key] or "") if existing else "")
            required = json.loads(values["required_capabilities"] or "[]")
            if not isinstance(required, list) or not all(isinstance(value, str) for value in required):
                raise ValueError("Invalid required capabilities")
            if values["max_firmware"] and (not values["min_firmware"] or api.parse_version_components(values["max_firmware"]) <= api.parse_version_components(values["min_firmware"])):
                raise ValueError("Invalid firmware compatibility range")
            connection.execute("""INSERT INTO webapp_builds
                (branch, version, type, file_path, basepath, comment, min_firmware, max_firmware, api_min, api_max, required_capabilities, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch, version) DO UPDATE SET comment=excluded.comment,
                    min_firmware=excluded.min_firmware, max_firmware=excluded.max_firmware,
                    api_min=excluded.api_min, api_max=excluded.api_max,
                    required_capabilities=excluded.required_capabilities, sha256=excluded.sha256""",
                (branch, args.version, args.type, str(directory), basepath, text or (existing["comment"] if existing else ""),
                 values["min_firmware"], values["max_firmware"], values["api_min"], values["api_max"], json.dumps(required), checksum))
        print(f"Published webapp to SQLite: {branch} {args.version}")
        return

    if args.action == "cull":
        print("Retention deferred: preserving firmware, bridge webapps and files until upgrade-path retention is qualified")
        return
    if args.action == "delete":
        with api.get_db_connection() as connection:
            query = "DELETE FROM firmware_builds WHERE soc = ? AND type = ? AND branch = ?"
            parameters = [args.soc.lower(), args.type.lower(), args.branch.lower()]
            if args.version:
                query += " AND version = ?"
                parameters.append(args.version)
            cursor = connection.execute(query, parameters)
            print(f"Removed {cursor.rowcount} database records; asset files retained")
        return
    with api.get_db_connection() as connection:
        if args.action == "list-webapp":
            rows = [dict(row) for row in connection.execute("SELECT branch, version, type, basepath, min_firmware, max_firmware FROM webapp_builds")]
        else:
            rows = [dict(row) for row in connection.execute("SELECT soc, type, branch, version, url FROM firmware_builds")]
    rows = [row for row in rows if all(not getattr(args, key, None) or row.get(key) == getattr(args, key) for key in ("soc", "type", "branch"))]
    rows.sort(key=lambda row: api.version_sort_key(row["version"]), reverse=True)
    if args.action == "list" and not args.history:
        latest = {}
        for row in rows:
            latest.setdefault((row["soc"], row["type"], row["branch"]), row)
        rows = list(latest.values())
    print(json.dumps(rows, indent=2))


def main():
    try:
        database_main()
    except (ValueError, OSError) as error:
        print(f"Publication failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
