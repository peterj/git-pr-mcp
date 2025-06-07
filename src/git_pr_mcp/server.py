import subprocess
import logging
from typing import Optional, Annotated
from mcp.server.fastmcp import FastMCP, Context
import os
from github import Github, Auth
import tempfile
import re
import shutil
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATE_FILE = "active_repo_state.json"

# Global state for the active repository
active_repo_details = {
    "path": None,  # Local file system path to the cloned repo
    "url": None,   # Original URL of the cloned repo
    "owner": None, # Repository owner (e.g., username or org)
    "name": None   # Repository name
}

def _save_state():
    """Saves the current active_repo_details to the state file."""
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(active_repo_details, f, indent=4)
        logger.info(f"Saved active repository state to {STATE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save state to {STATE_FILE}: {str(e)}")

def _load_state():
    """Loads active_repo_details from the state file if it exists and is valid."""
    global active_repo_details
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                loaded_details = json.load(f)
            # Validate that the path (if present) still exists
            if loaded_details.get("path") and os.path.isdir(loaded_details["path"]):
                active_repo_details = loaded_details
                logger.info(f"Loaded active repository state from {STATE_FILE}: {active_repo_details['path']}")
            elif loaded_details.get("path"):
                logger.warning(f"State file {STATE_FILE} references a path that no longer exists: {loaded_details['path']}. Ignoring.")
                # Optionally, delete the state file or clear its path entry if it's invalid
                # For now, just ignore and start fresh. The next clone will overwrite.
            else:
                logger.info(f"State file {STATE_FILE} loaded but no valid path found. Starting fresh.")
        else:
            logger.info(f"{STATE_FILE} not found. Starting with a fresh state.")
    except Exception as e:
        logger.error(f"Failed to load state from {STATE_FILE}: {str(e)}. Starting with a fresh state.")

if not os.getenv("GITHUB_TOKEN"):
    raise ValueError("GITHUB_TOKEN environment variable is not set. PyGithub tools will not work.")

auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
g: Github = Github(auth=auth)

mcp = FastMCP("git-pr-mcp")
_load_state()

# Configure git user identity and GitHub OAuth token
def _configure_git():
    """Configure git user identity and GitHub token for authentication."""
    try:
        # Configure git user identity from environment variables
        git_user_name = os.getenv("GIT_USER_NAME")
        git_user_email = os.getenv("GIT_USER_EMAIL")

        if git_user_name and git_user_email:
            subprocess.run(["git", "config", "--global", "user.name", git_user_name], check=True)
            subprocess.run(["git", "config", "--global", "user.email", git_user_email], check=True)
            logger.info(f"Configured git user identity: {git_user_name} <{git_user_email}>")
        else:
            logger.warning("GIT_USER_NAME and/or GIT_USER_EMAIL not set - git commits may fail")

        # Configure GitHub OAuth token for HTTPS authentication
        github_token = os.getenv("GITHUB_TOKEN")
        if github_token:
            # Set up credential helper to use the GitHub token
            subprocess.run(["git", "config", "--global", "credential.helper", "store"], check=True)
            # Configure GitHub credentials - this uses the token as password with username as token
            subprocess.run([
                "git", "config", "--global",
                "url.https://oauth2:" + github_token + "@github.com/.insteadOf",
                "https://github.com/"
            ], check=True)
            logger.info("Configured git to use GitHub token for HTTPS authentication")
        else:
            logger.warning("GITHUB_TOKEN not found - git push operations may fail")

    except Exception as e:
        logger.warning(f"Failed to configure git settings: {str(e)}")

_configure_git()

# Helper function to parse repo URL (simplistic)
def _parse_repo_url(repo_url: str) -> tuple[Optional[str], Optional[str]]:
    # Regex to capture owner and repo name from common Git URL patterns
    # Supports https://github.com/owner/repo.git, git@github.com:owner/repo.git, https://github.com/owner/repo
    # The regex captures the owner and the repo name (which might include .git suffix).
    # The .git suffix is then removed in Python code.
    pattern = r"(?:https?://[^/]+/|git@[\w.-]+:)([^/]+)/([^/]+)$" 
    match = re.search(pattern, repo_url)
    if match:
        owner = match.group(1)
        name_with_suffix = match.group(2)
        if name_with_suffix.endswith(".git"):
            name = name_with_suffix[:-4]  # Remove .git suffix
        else:
            name = name_with_suffix
        return owner, name
    logger.warning(f"Could not parse owner and repo name from URL: {repo_url}")
    return None, None

@mcp.tool(
    name="get_git_status",
    description="Get the current git status of the repository",
)
def get_git_status(
    ctx: Context,
    repo_path: Annotated[Optional[str], "Path to the git repository (optional, defaults to current directory)"] = ".",
) -> str:
    """Get the current git status of the repository."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        ctx.info(f"Getting git status for {repo_path}")
        
        if result.returncode == 0:
            status_output = result.stdout.strip()
            if not status_output:
                return "Repository is clean - no changes detected."
            else:
                return f"Git Status:\n{status_output}"
        else:
            return f"Error getting git status: {result.stderr}"
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Error running git status: {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="list_branches",
    description="List all branches in the repository",
)
def list_branches(
    ctx: Context,
    repo_path: Annotated[Optional[str], "Path to the git repository (optional, defaults to current directory)"] = ".",
    remote: Annotated[bool, "Include remote branches"] = False,
) -> str:
    """List all branches in the repository."""
    try:
        cmd = ["git", "branch", "-v"]
        if remote:
            cmd.append("-a")
            
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        ctx.info(f"Listing branches for {repo_path} (remote: {remote})")
        
        if result.returncode == 0:
            branches = result.stdout.strip()
            return f"Branches:\n{branches}"
        else:
            return f"Error listing branches: {result.stderr}"
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Error running git branch: {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="create_pr_summary",
    description="Create a summary for a pull request based on git diff",
)
def create_pr_summary(
    base_branch: Annotated[str, "Base branch to compare against"],
    ctx: Context,
    head_branch: Annotated[Optional[str], "Head branch (optional, defaults to current branch)"] = None,
    repo_path: Annotated[Optional[str], "Path to the git repository (optional, defaults to current directory)"] = ".",
) -> str:
    """Create a summary for a pull request based on git diff."""
    try:
        # Get current branch if head_branch not specified
        if not head_branch:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            head_branch = result.stdout.strip()
        
        ctx.info(f"Creating PR summary: {head_branch} -> {base_branch}")
        
        # Get diff between branches
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...{head_branch}", "--stat"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        if result.returncode == 0:
            diff_stat = result.stdout.strip()
            if not diff_stat:
                return f"No differences found between {base_branch} and {head_branch}"
            else:
                return f"PR Summary ({head_branch} -> {base_branch}):\n\nChanges:\n{diff_stat}"
        else:
            return f"Error creating PR summary: {result.stderr}"
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Error creating PR summary: {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="get_commit_history",
    description="Get commit history for a branch or between branches",
)
def get_commit_history(
    ctx: Context,
    branch: Annotated[Optional[str], "Branch name (optional, defaults to current branch)"] = None,
    limit: Annotated[int, "Maximum number of commits to return"] = 10,
    repo_path: Annotated[Optional[str], "Path to the git repository (optional, defaults to current directory)"] = ".",
) -> str:
    """Get commit history for a branch."""
    try:
        cmd = ["git", "log", f"--max-count={limit}", "--oneline"]
        if branch:
            cmd.append(branch)
            
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        branch_info = f" for branch '{branch}'" if branch else ""
        ctx.info(f"Getting commit history{branch_info} (limit: {limit})")
        
        if result.returncode == 0:
            commits = result.stdout.strip()
            if not commits:
                return "No commits found"
            else:
                return f"Recent commits{branch_info}:\n{commits}"
        else:
            return f"Error getting commit history: {result.stderr}"
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Error getting commit history: {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="get_git_diff",
    description="Get git diff between commits, branches, or working directory",
)
def get_git_diff(
    ctx: Context,
    target: Annotated[Optional[str], "Target to diff against (commit hash, branch name, etc.). Defaults to working directory vs HEAD"] = None,
    repo_path: Annotated[Optional[str], "Path to the git repository (optional, defaults to current directory)"] = ".",
) -> str:
    """Get git diff between commits, branches, or working directory."""
    try:
        cmd = ["git", "diff"]
        if target:
            cmd.append(target)
            
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        diff_target = f" against {target}" if target else " (working directory vs HEAD)"
        ctx.info(f"Getting git diff{diff_target}")
        
        if result.returncode == 0:
            diff_output = result.stdout.strip()
            if not diff_output:
                return f"No differences found{diff_target}"
            else:
                return f"Git Diff{diff_target}:\n{diff_output}"
        else:
            return f"Error getting git diff: {result.stderr}"
            
    except subprocess.CalledProcessError as e:
        error_msg = f"Error running git diff: {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="clone_repository",
    description="Clones a GitHub repository into a new temporary local directory, cleans up any previous one, saves state, and sets it as the active repository. Parses owner/name from URL.",
)
def clone_repository(
    ctx: Context,
    repo_url: Annotated[str, "The URL of the GitHub repository (e.g., https://github.com/user/repo.git)"],
) -> str:
    """Clones a GitHub repository to a new temporary directory, cleans up old, saves state, and sets as active."""
    global active_repo_details
    
    # Clean up previous active repository's directory if it exists
    if active_repo_details["path"] and os.path.exists(active_repo_details["path"]):
        previous_repo_path = active_repo_details["path"]
        ctx.info(f"Cleaning up previous active repository directory: {previous_repo_path}")
        try:
            shutil.rmtree(previous_repo_path)
            ctx.info(f"Successfully removed {previous_repo_path}")
        except Exception as e:
            ctx.warning(f"Failed to remove previous repository directory {previous_repo_path}: {str(e)}")
            # Decide if this should be a fatal error or just a warning. For now, warning.

    # Reset active_repo_details before attempting a new clone and save this cleared state
    active_repo_details = {"path": None, "url": None, "owner": None, "name": None}
    _save_state()
    temp_dir = None

    try:
        # Create a new temporary directory for the clone
        temp_dir = tempfile.mkdtemp(prefix="mcp_clone_")
        ctx.info(f"Created new temporary directory for clone: {temp_dir}")

        cmd = ["git", "clone", repo_url, temp_dir]
        
        ctx.info(f"Cloning repository {repo_url} to {temp_dir}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True 
        )
        
        if result.returncode == 0:
            owner, name = _parse_repo_url(repo_url)
            active_repo_details["path"] = temp_dir
            active_repo_details["url"] = repo_url
            active_repo_details["owner"] = owner
            active_repo_details["name"] = name
            _save_state()
            
            msg = f"Repository {repo_url} cloned successfully to {temp_dir} and set as active. State saved."
            if owner and name:
                msg += f" Parsed owner: '{owner}', name: '{name}'."
            else:
                msg += " Could not parse owner/name from URL for GitHub operations."
            ctx.info(msg)
            return msg
        else:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return f"Error cloning repository: {result.stderr}"

    except subprocess.CalledProcessError as e:
        error_msg = f"Error cloning repository {repo_url}: {e.stderr if e.stderr else str(e)}"
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                ctx.info(f"Cleaned up temporary directory {temp_dir} after failed clone.")
            except Exception as cleanup_e:
                ctx.warning(f"Failed to cleanup temporary directory {temp_dir} after failed clone: {str(cleanup_e)}")
        ctx.error(error_msg)
        return error_msg
    except FileNotFoundError:
        error_msg = "Error: Git command not found. Please ensure Git is installed and in your PATH."
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred during clone: {str(e)}"
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                ctx.info(f"Cleaned up temporary directory {temp_dir} after unexpected error during clone.")
            except Exception as cleanup_e:
                ctx.warning(f"Failed to cleanup temporary directory {temp_dir} after unexpected error: {str(cleanup_e)}")
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="create_git_branch",
    description="Creates a new branch in the active local git repository.",
)
def create_git_branch(
    ctx: Context,
    branch_name: Annotated[str, "The name of the new branch to create"],
    base_branch: Annotated[Optional[str], "The base branch to create the new branch from (optional, defaults to current HEAD)"] = None,
) -> str:
    """Creates a new branch in the active local git repository from a specified base branch."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."
    
    repo_path = active_repo_details["path"]

    try:
        cmd = ["git", "checkout", "-b", branch_name]
        if base_branch:
            cmd.append(base_branch)
        
        ctx.info(f"Creating new branch '{branch_name}' from '{base_branch if base_branch else 'current HEAD'}' in active repo {repo_path}")
        
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        if result.returncode == 0:
            return f"Branch '{branch_name}' created successfully and checked out from '{base_branch if base_branch else 'current HEAD'}' in {repo_path}."
        else:
            return f"Error creating branch '{branch_name}': {result.stderr}"

    except subprocess.CalledProcessError as e:
        error_msg = f"Error creating branch '{branch_name}': {e.stderr if e.stderr else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except FileNotFoundError:
        error_msg = "Error: Git command not found. Please ensure Git is installed and in your PATH."
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        ctx.error(error_msg)
        return error_msg 


@mcp.tool(
    name="git_commit_changes",
    description="Stages all changes (git add .) and commits them with a given message in the active repository.",
)
def git_commit_changes(
    ctx: Context,
    commit_message: Annotated[str, "The commit message"],
) -> str:
    """Stages all changes in the active repo and commits them with a given message."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."
    
    repo_path = active_repo_details["path"]

    try:
        # Stage all changes
        ctx.info(f"Staging all changes in active repo: {repo_path}")
        add_cmd = ["git", "add", "."]
        add_result = subprocess.run(
            add_cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        if add_result.returncode != 0:
            # This typically won't be hit with check=True, but kept for robustness
            error_msg = f"Error staging changes in {repo_path}: {add_result.stderr}"
            ctx.error(error_msg)
            return error_msg

        # Commit changes
        ctx.info(f"Committing changes in active repo ({repo_path}) with message: '{commit_message}'")
        commit_cmd = ["git", "commit", "-m", commit_message]
        commit_result = subprocess.run(
            commit_cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        if commit_result.returncode == 0:
            # Check if there was anything to commit
            if "nothing to commit, working tree clean" in commit_result.stdout or \
               "no changes added to commit" in commit_result.stdout:
                return f"No changes to commit in active repo ({repo_path}). Working tree clean."
            return f"Changes committed successfully in active repo ({repo_path}) with message: '{commit_message}'."
        else:
            # This typically won't be hit with check=True
            return f"Error committing changes: {commit_result.stderr}"

    except subprocess.CalledProcessError as e:
        # Check if the error is due to nothing to commit, which can happen if `git add .` stages nothing
        # and `git commit` then fails. Some git versions might output this to stdout, some to stderr.
        output = e.stdout.strip() if e.stdout else ""
        err_output = e.stderr.strip() if e.stderr else ""
        if "nothing to commit" in output or "nothing to commit" in err_output or \
           "no changes added to commit" in output or "no changes added to commit" in err_output:
            return f"No changes to commit in active repo ({repo_path}). Working tree clean."
        error_msg = f"Error during git operation in active repo ({repo_path}): {err_output if err_output else output if output else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except FileNotFoundError:
        error_msg = "Error: Git command not found. Please ensure Git is installed and in your PATH."
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        ctx.error(error_msg)
        return error_msg 


@mcp.tool(
    name="git_push_branch",
    description="Pushes a local branch from the active repository to the remote (origin).",
)
def git_push_branch(
    ctx: Context,
    branch_name: Annotated[str, "The name of the local branch to push"],
    set_upstream: Annotated[bool, "Set the upstream for the branch (git push -u origin <branch_name>)"] = True,
) -> str:
    """Pushes a local branch from the active repository to the remote (origin)."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."
    
    repo_path = active_repo_details["path"]

    try:
        cmd = ["git", "push"]
        if set_upstream:
            cmd.extend(["-u", "origin", branch_name])
        else:
            cmd.extend(["origin", branch_name])
        
        ctx.info(f"Pushing branch '{branch_name}' to origin from active repo {repo_path} (set_upstream: {set_upstream})")
        
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        
        if result.returncode == 0:
            # Git push can have varied output, often to stderr even on success (e.g. when up-to-date or new branch)
            # We rely on check=True to catch actual errors. If no error, assume success.
            success_msg = f"Branch '{branch_name}' pushed to origin successfully from active repo ({repo_path})."
            if result.stdout:
                success_msg += f"\nOutput:\n{result.stdout.strip()}"
            if result.stderr: # Some info like 'branch X set up to track Y' goes to stderr
                success_msg += f"\nInfo:\n{result.stderr.strip()}"
            return success_msg
        else:
            # This part is unlikely to be reached if check=True raises CalledProcessError
            return f"Error pushing branch '{branch_name}': {result.stderr}"

    except subprocess.CalledProcessError as e:
        error_msg = f"Error pushing branch '{branch_name}' in active repo ({repo_path}): {e.stderr if e.stderr else e.stdout if e.stdout else str(e)}"
        ctx.error(error_msg)
        return error_msg
    except FileNotFoundError:
        error_msg = "Error: Git command not found. Please ensure Git is installed and in your PATH."
        ctx.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        ctx.error(error_msg)
        return error_msg 


@mcp.tool(
    name="create_github_pr",
    description="Creates a pull request on GitHub for the active repository using PyGithub.",
)
def create_github_pr(
    ctx: Context,
    title: Annotated[str, "The title of the pull request"],
    body: Annotated[str, "The body/description of the pull request"],
    base_branch: Annotated[str, "The branch to merge into (e.g., main, develop)"],
    head_branch: Annotated[str, "The branch containing the changes to be merged (must be pushed to remote)"],
) -> str:
    """Creates a pull request on GitHub for the active repository using PyGithub."""
    global active_repo_details
    if not active_repo_details["owner"] or not active_repo_details["name"]:
        err_msg = "Error: Active repository details (owner/name) not found or incomplete. "
        err_msg += "Ensure the repository was cloned successfully and owner/name could be parsed from the URL."
        if not active_repo_details["path"]:
            err_msg = "Error: No active repository. Please clone a repository first using 'clone_repository'."
        return err_msg

    owner = active_repo_details["owner"]
    repo_name = active_repo_details["name"]
    
    try:
        repo_full_name = f"{owner}/{repo_name}"
        ctx.info(f"Attempting to create GitHub PR for active repo {repo_full_name}: '{head_branch}' -> '{base_branch}' with title '{title}'")

        gh_repo = g.get_repo(repo_full_name)
        
        pull_request = gh_repo.create_pull(
            title=title,
            body=body,
            head=head_branch, # The name of the branch where your changes are implemented.
            base=base_branch  # The name of the branch you want the changes pulled into.
            # draft=False, # Optional: set to True to create a draft PR
            # maintainer_can_modify=True, # Optional
        )
        
        ctx.info(f"Successfully created PR: {pull_request.html_url}")
        return f"Successfully created PR: {pull_request.html_url}"

    except Exception as e:
        # PyGithub raises github.GithubException for API errors
        repo_identifier = f"{active_repo_details.get('owner', '?')}/{active_repo_details.get('name', '?')}" # Safe access for error reporting
        error_msg = f"Error creating GitHub PR for active repo {repo_identifier} ('{head_branch}' -> '{base_branch}'): {str(e)}" # Adjusted error message
        # The exception 'e' from PyGithub often contains useful 'data' and 'status'
        if hasattr(e, 'data') and e.data and 'message' in e.data:
            error_msg += f" - API Message: {e.data['message']}"
            if 'errors' in e.data:
                 error_msg += f" - Details: {e.data['errors']}"
        elif hasattr(e, 'status'):
             error_msg += f" - Status: {e.status}"
        
        ctx.error(error_msg)
        return error_msg 


@mcp.tool(
    name="read_file_in_repo",
    description="Reads the content of a specified file within the active repository.",
)
def read_file_in_repo(
    ctx: Context,
    relative_file_path: Annotated[str, "The path of the file relative to the active repository root (e.g., src/my_module.py)"],
) -> str:
    """Reads the content of a specified file in the active repo."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."

    base_path = active_repo_details["path"]
    full_file_path = os.path.join(base_path, relative_file_path)

    try:
        ctx.info(f"Attempting to read file in active repo: {full_file_path}")
        
        if not os.path.exists(full_file_path):
            not_found_msg = f"Error: File not found at {full_file_path}"
            ctx.warning(not_found_msg)
            return not_found_msg
            
        if not os.path.isfile(full_file_path):
            not_file_msg = f"Error: Path exists but is not a file: {full_file_path}"
            ctx.warning(not_file_msg)
            return not_file_msg

        with open(full_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        success_msg = f"Successfully read content from {full_file_path}"
        # Optionally, log a snippet of the content for very small files, or just its length
        # For now, just log success and return the full content.
        # ctx.info(success_msg + f" (length: {len(content)})") 
        ctx.info(success_msg)
        return content

    except Exception as e:
        error_msg = f"Error reading file {full_file_path} in active repo: {str(e)}"
        ctx.error(error_msg)
        return error_msg


@mcp.tool(
    name="write_file_in_repo",
    description="Creates a new file or overwrites an existing file with specified content within the active repository. Ensures parent directories are created.",
)
def write_file_in_repo(
    ctx: Context,
    relative_file_path: Annotated[str, "The path of the file relative to the active repository root (e.g., src/my_module.py)"],
    content: Annotated[str, "The string content to write to the file"],
) -> str:
    """Creates/overwrites a file in the active repo with specified content, ensuring parent directories exist."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."
    
    base_path = active_repo_details["path"]

    try:
        full_file_path = os.path.join(base_path, relative_file_path)
        
        ctx.info(f"Attempting to write file in active repo: {full_file_path}")
        
        # Ensure parent directory exists
        parent_dir = os.path.dirname(full_file_path)
        if parent_dir: # Check if parent_dir is not an empty string (e.g. for top-level files)
            os.makedirs(parent_dir, exist_ok=True)
            ctx.info(f"Ensured directory exists: {parent_dir}")
        
        with open(full_file_path, 'w', encoding='utf-8') as f:
            f.write(content)
            
        success_msg = f"Successfully wrote content to {full_file_path}"
        ctx.info(success_msg)
        return success_msg

    except Exception as e:
        error_msg = f"Error writing file {os.path.join(base_path, relative_file_path) if active_repo_details['path'] else relative_file_path} in active repo: {str(e)}"
        ctx.error(error_msg)
        return error_msg 


@mcp.tool(
    name="list_files_in_repo",
    description="Lists all files within the active repository, providing their paths relative to the repo root.",
)
def list_files_in_repo(
    ctx: Context,
) -> str:
    """Lists all files in the active repository."""
    global active_repo_details
    if not active_repo_details["path"]:
        return "Error: No active repository. Please clone a repository first using 'clone_repository'."

    repo_root_path = active_repo_details["path"]
    file_list = []

    try:
        ctx.info(f"Listing all files in active repo: {repo_root_path}")
        for root, _, files in os.walk(repo_root_path):
            for file_name in files:
                full_path = os.path.join(root, file_name)
                relative_path = os.path.relpath(full_path, repo_root_path)
                file_list.append(relative_path)
        
        if not file_list:
            return "No files found in the active repository."
        
        # Return as a newline-separated string for readability
        return "Files in repository:\n" + "\n".join(sorted(file_list))

    except Exception as e:
        error_msg = f"Error listing files in active repo {repo_root_path}: {str(e)}"
        ctx.error(error_msg)
        return error_msg 