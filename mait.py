#!/usr/bin/env python3
try:
    from typing import cast
    import sys
    import os
    import subprocess
    import re
    import argparse
    from time import sleep, monotonic
except KeyboardInterrupt:
    print(" KeyboardInterrupt")
    quit()


VERBOSE_LEN = 20
YOUR_SITE_URL = ""
YOUR_APP_NAME = "muxmait"
DEFAULT_MODEL = "gemini/gemini-flash-lite-latest"
# git mode (-g): every attempt in the fallback chain gets GIT_TIMEOUT
# seconds of its own, so a slow model is abandoned for the next one instead
# of stalling the prompt.
GIT_TIMEOUT = 10

args: argparse.Namespace

default_system_prompt = """
You are an AI assistant within a shell command 'mait'. You operate by reading the
users scrollback. You can not see interactive input. Here are your guidelines:

DO ensure you present one command per response at the end, in a code block:
  ```bash
  command
  ```

DO NOT use multiple code blocks. For multiple commands, join with semicolons:
  ```bash
  command1; command2
  ```

DO precede commands with brief explanations.

DO NOT rely on your own knowledge; use `command --help` or `man command | cat`
  so both you and the user understand what is happening.

DO give a command to gather information when needed.

Do NOT suggest interactive editors like nano or vim, or other interactive programs.

DO use commands like `sed` or `echo >>` for file edits, or other non-interactive commands where applicable.

DO NOT add anything after command

If no command seems necessary, gather info or give a command for the user to explore.
"""

make_google_search_sys_prompt = """
Turn this terminal output and/or user question into an effective google search.
Remember only 35 words max. Return one query.
Remove any identifying info or specific file paths.
"""


def clean_command(c: str) -> str:
    subs = {
            '"': '\\"',
            "\n": " ",
            "$": "\\$",
            "`": "\\`",
            "\\": "\\\\",
            }
    return "".join(subs.get(x, x) for x in c)


def get_response_debug(prompt: str, system_prompt: str, model: str) -> str:
    if args.verbose:
        print("raw input")
        print("------------------------------------------")
        print("\n".join("# "+line for line in prompt.splitlines()))
        print("------------------------------------------")
    response = ""
    response += "sys prompt len:".ljust(VERBOSE_LEN) + str(len(system_prompt))
    response += "requested model:".ljust(VERBOSE_LEN) + model
    response += "prompt len:".ljust(VERBOSE_LEN) + str(len(prompt)) + "\n"
    response += "prefix_input:".ljust(VERBOSE_LEN) +\
                prompt.splitlines()[0:-1][0] + "\n"
    response += "test code block:\n"
    response += "```bash\n echo \"$(" + prompt.splitlines()[0:-1][0] + ")\"\n```\n"
    return response


def get_response_litellm(prompt: str, system_prompt: str, model: str,
                         timeout: float | None = None) -> str:
    import litellm
    from litellm.types.utils import ModelResponse
    from litellm.types.llms.anthropic import AnthropicThinkingParam
    litellm.drop_params = True
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]
    if args.thinking_level == "minimal":
        thinking_params = {"type": "disabled"}
    else:
        # For litellm, we'll try to map thinking level if possible, 
        # or just enable it.
        thinking_params = {"type": "enabled"}
    litellm_thinking: "AnthropicThinkingParam" = cast(
        AnthropicThinkingParam, thinking_params
    )

    response = cast(ModelResponse, litellm.completion(
        model=model,
        messages=messages,
        temperature=1,
        stop=["```\n"],
        thinking=litellm_thinking,
        timeout=timeout
    ))

    try:
        return response['choices'][0]['message']['content']
    except (AttributeError, KeyError):
        raise RuntimeError(f"LiteLLM model response error: {response}")


def _post_within(url: str, headers: dict, data: dict, deadline: float,
                 model: str):
    """POST and return a Response, giving up at `deadline`.

    requests' timeout only bounds the gap between individual reads, so a
    server that keeps dribbling bytes can run far past it.  Doing the call on
    a worker thread lets the caller stop waiting at the deadline no matter
    what the socket is doing.
    """
    import queue
    import threading
    import requests

    results: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

    def run():
        try:
            remaining = deadline - monotonic()
            results.put(("ok", requests.post(url, headers=headers, json=data,
                                             timeout=max(remaining, 0.1))))
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            results.put(("err", e))

    # daemon: an abandoned model is left to finish (or not) in the background
    threading.Thread(target=run, daemon=True).start()

    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{model}: no response within the time budget")
    try:
        kind, value = results.get(timeout=remaining)
    except queue.Empty:
        raise TimeoutError(
            f"{model}: no complete response within the time budget") from None
    if kind == "err":
        raise cast(Exception, value)
    return cast("requests.Response", value)


def _error_info(body):
    """Pull (message, code) out of an API error body of any shape.

    OpenRouter answers {"error": {...}}, the Gemini OpenAI-compatible
    endpoint answers [{"error": {...}}], and some endpoints answer
    {"error": "string"}.  Treating every body as a dict raised AttributeError
    on the other shapes and hid the provider's actual reason for failing.
    """
    if isinstance(body, list):
        for item in body:
            message, code = _error_info(item)
            if message is not None or code is not None:
                return message, code
        return None, None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message"), err.get("code")
        if isinstance(err, str):
            return err, body.get("code")
        return body.get("message"), body.get("code")
    return None, None


def get_response_direct(prompt: str, system_prompt: str, model: str,
                         timeout: float | None = None) -> str:
    import requests

    api_key = os.getenv(direct_models[model]["api_key"])
    base_url = direct_models[model]["base_url"]

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    req_model = model[model.find("/")+1:]
    if model == "openrouter/free" or req_model in ("free", "auto"):
        req_model = model

    # OpenRouter routers (openrouter/free, ...) pick a random model per
    # request: an unlucky pick can reject optional sampling params (400) or
    # be rate limited / unavailable (429, 503, 5xx).  Retry a few times;
    # on retry send the minimal documented payload.
    max_attempts = 3 if req_model.startswith("openrouter/") else 1
    response: requests.Response | None = None
    deadline = monotonic() + timeout if timeout is not None else None
    send_thinking = "gemini" in model.lower() or "gemini" in base_url.lower()
    attempt = 0
    while attempt < max_attempts:
        remaining = None if deadline is None else deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f"{model}: gave up after {timeout:.0f}s")
        data = {
            "model": req_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
        }
        if attempt == 0:
            data["temperature"] = 1.0
            data["stop"] = ["```\n"]
        if send_thinking:
            data["reasoning_effort"] = args.thinking_level

        if deadline is None:
            response = requests.post(url, headers=headers, json=data)
        else:
            response = _post_within(url, headers, data, deadline, model)
        try:
            res_json = response.json()
        except ValueError:
            res_json = response.text

        if response.status_code == 200:
            if isinstance(res_json, dict) and res_json.get("choices"):
                return res_json["choices"][0]["message"]["content"]
            message, code = _error_info(res_json)
            if message is not None or code is not None:
                raise RuntimeError(f"API Error ({message or code})")
            raise RuntimeError(f"Unexpected response structure: {res_json}")

        # surface the provider's reason instead of a bare status code
        message, code = _error_info(res_json)
        if message is None:
            message = response.text
        code_str = f" (code {code})" if code is not None else ""

        # some models reject reasoning_effort for any thinking level ("MINIMAL
        # is not supported"): drop it and retry the same model once before
        # giving up on it
        if (response.status_code == 400 and send_thinking
                and "thinking level" in str(message).lower()):
            send_thinking = False
            continue
        if (attempt + 1 < max_attempts
                and (response.status_code in (400, 429, 503)
                     or response.status_code >= 500)):
            attempt += 1
            continue
        raise RuntimeError(f"API Error {response.status_code}{code_str}: {message}")
    # Unreachable when max_attempts >= 1; kept for type-correctness.
    if response is not None:
        raise RuntimeError(f"API Error {response.status_code}: {response.text}")
    raise RuntimeError("API request failed: no response")


def get_response(prompt: str, system_prompt: str, model: str,
                 timeout: float | None = None) -> str:
    if args.verbose:
        print("getting response")
        print(f"using model {model}")
        print("use litellm " + str(model not in direct_models))
    response: str
    if args.debug:
        response = get_response_debug(prompt, system_prompt, model)
    elif model in direct_models:
        response = get_response_direct(prompt, system_prompt, model, timeout)
    else:
        response = get_response_litellm(prompt, system_prompt, model, timeout)
    if args.verbose:
        print("raw response")
        print("------------------------------------------")
        print(response)
        print("------------------------------------------")

    if args.log is not None:
        with open(args.log, 'a') as log:
            log.write(response)

    return response


def extract_command(response: str) -> str:

    code_blocks = re.findall(r"```(?:bash|shell)?\n(.+?)\n",
                             response, re.DOTALL)
    if args.verbose:
        print("code_blocks:".ljust(VERBOSE_LEN) + ":".join(code_blocks))
    if code_blocks:
        # Get the last line from the last code block
        command = code_blocks[-1].strip().split("\n")[-1]
    else:
        # just take last line as command if no code block
        command = response.strip().splitlines()[-1]

    return command


def process_prompt(prompt: str, system_prompt: str, model: str):
    response = None
    if args.git:
        git_fallback_models = [
            "gemini/gemini-3-flash-preview",
            "gemini/gemini-3.1-flash-lite-preview",
            "openrouter/nvidia/nemotron-3.5-lightning:free",
            "gemini/gemini-flash-lite-latest",
            "gemini/gemini-3.5-flash-lite",
            "openrouter/free",
        ]
        models_to_try = list(git_fallback_models)
        if model not in models_to_try:
            models_to_try.insert(0, model)
        elif getattr(args, "model_explicit", False):
            # -m was given on the command line: honour it first, then fall back
            models_to_try.remove(model)
            models_to_try.insert(0, model)

        for m in models_to_try:
            try:
                if args.verbose:
                    print(f"Trying git model: {m}")
                response = get_response(prompt, system_prompt, m,
                                        timeout=GIT_TIMEOUT)
                break
            except Exception as e:
                print(f"Model {m} failed/rejected: {e}")
                print("Switching to next model in fallback list...")

        if response is None:
            print("All fallback models failed for git operation.")
            quit()
    else:
        try:
            response = get_response(prompt, system_prompt, model)
        except Exception as e:
            print("unexpected output")
            print(e)
            quit()

    # Extract a command from the response
    command = extract_command(response)
    # Look for the last code block

    if not args.quiet:
        print("\n")
        response = re.sub(r"```.*?\n.*?\n", "", response, flags=re.DOTALL)
        response = re.sub(rf"{re.escape(command)}", "", response, flags=re.DOTALL)
        print(response)

    # add command to Shell Prompt
    if command:
        put_command(command)


def put_command(command: str):
    if args.log_commands is not None:
        with open(args.log_commands, 'a') as f:
            f.write(command+"\n")

    # --no-paste: print/log only, never send keys to tmux
    if args.no_paste:
        if args.verbose:
            print("no-paste: not sending command to tmux")
        return

    # presses enter on target tmux pane
    enter = "ENTER" if args.auto else ""
    # allows user to repeatedly call ai with the same options
    if args.recursive:
        if args.target == default_tmux_target:
            command = command + ";mait " + " ".join(sys.argv[1:])
        else:
            subprocess.run(
                    f'tmux send-keys "mait {" ".join(sys.argv[1:])}" {enter}',
                    shell=True
                    )
            print("\n")

    # send command to shell prompt via tmux buffer paste
    try:
        subprocess.run(["tmux", "set-buffer", "--", command], check=True)
        if args.target == default_tmux_target:
            subprocess.Popen(f'sleep 0.05 && tmux paste-buffer -p -t {args.target}', shell=True)
        else:
            subprocess.run(["tmux", "paste-buffer", "-p", "-t", args.target], check=True)
    except Exception:
        cleaned = clean_command(command)
        if args.target == default_tmux_target:
            subprocess.Popen(f'sleep 0.05 && tmux send-keys -t {args.target} "{cleaned}"', shell=True)
        else:
            subprocess.run(f'tmux send-keys -t {args.target} "{cleaned}"', shell=True)

    # a delay when using auto so user can hopefully C-c out
    if args.auto:
        sleep(args.delay)

        subprocess.run(f'tmux send-keys -t {args.target}  {enter}', shell=True)


headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36',
            }


def extract_qa(html_content: str) -> str:

    from bs4 import BeautifulSoup
    # Parse the HTML content
    soup = BeautifulSoup(html_content, 'html.parser')

    # Extracting questions and answers
    questions = soup.find_all('div', class_='question')
    answers = soup.find_all('div', class_='answer')

    markup_output = []
    if len(questions) < 1 or len(answers) < 1:
        return ""
    a = questions[0].find('div', class_='s-prose')
    markup_output.append(f"### Question \n{a.get_text(strip=True)}\n" if a else "")

    for i, ans in enumerate(answers[:3], 1):

        answer_text = ans.find('div', class_='s-prose')
        # Find corresponding answers
        if answer_text:
            markup_output.append(f"**Answer: {i}**\n{answer_text.get_text()}\n")

    return '\n'.join(markup_output)


def google_search(query: str) -> list[str]:

    from bs4 import BeautifulSoup
    import requests
    # Constructing the URL for Google search
    url = f"https://www.google.com/search?q={query}&num=10"

    # Send the request
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    # Parse the HTML content
    soup = BeautifulSoup(response.text, 'html.parser')

    # Find all the search result divs
    search_results = soup.find_all('div', class_='tF2Cxc')

    results = []
    for result in search_results[:10]:  # We only want the top 10
        try:
            link_el = result.find('a')
            # Extract link. Note: Google links are often redirects,
            # this gets the actual link shown
            link = link_el['href'] if link_el is not None and 'href' in link_el.attrs else None
            if link is None:
                continue

            results.append(link)
        except Exception:
            continue

    return results


def get_stack_answers(question: str) -> str:

    import requests
    res = google_search(question)
    p = ""
    for r in res:
        r = (requests.get(r, headers=headers))
        if r.status_code != 200:
            continue
        p = extract_qa(r.text)
        if len(p) > 5:
            break

    return p


def auto_overflow(prompt: str):
    """
    1. Use ai to formulate question based on scrollback and/or user input.
    2. Search google with question.
    3. Get first stack exchange link and parse it.
    """
    # First get AI to formulate a clear question

    question = get_response(prompt,
                            make_google_search_sys_prompt,
                            args.model_stackexchange)

    if args.verbose:
        print("Searching for: " + question)

    # Get stack overflow answers
    stack_content = get_stack_answers(question)

    if args.verbose:
        print("stack content")
        print("-"*80)
        print(stack_content)
        print("-"*80)
    # Combine original prompt with stack overflow content
    return f"""
{prompt}

Possibly Relevant Stack Overflow information, Only consider if relevant to users question:
{stack_content}

"""


def run_muxmait():

    global args

    args, arg_input = parser.parse_known_args()

    # let user select model from model list
    args.model_explicit = args.model is not None
    if args.model is None:
        args.model = DEFAULT_MODEL
    if args.model in model_dict:
        args.model = model_dict[args.model]
    elif len(args.model) < 4:
        print("Model quick list")
        for k, v in model_dict.items():
            print(f"{k}:    {v}")
        quit()
    if args.model_stackexchange in model_dict:
        args.model_stackexchange = model_dict[args.model_stackexchange]
    elif len(args.model_stackexchange) < 4:
        print("Model quick list")
        for k, v in model_dict.items():
            print(f"{k}:    {v}")
        quit()

    # custom system prompt
    if args.system_prompt is not None:
        with open(args.system_prompt) as f:
            input_system_prompt = f.read()
        if args.verbose:
            print("system prompt removed")
    else:
        input_system_prompt = default_system_prompt

    if args.git:
        args.no_screen = True
        args.quiet = True

    # get input from stdin or tmux scrollback
    input_string: str = ""
    if not sys.stdin.isatty():
        input_string += "User input piped in from command:\n"
        input_string += "".join(sys.stdin)
        input_string += "\n"
    elif args.git:
        try:
            status_out = subprocess.check_output("git status", shell=True).decode("utf-8")
            if status_out.strip():
                input_string += "Git status output:\n" + status_out + "\n"
        except Exception:
            pass
        try:
            diff_out = subprocess.check_output("git diff", shell=True).decode("utf-8")
            if diff_out.strip():
                diff_size = len(diff_out)
                if diff_size > args.max_diff:
                    keep = args.max_diff
                    # cut at a line boundary so the model sees whole hunks
                    cutoff = diff_out.rfind("\n", 0, keep) + 1
                    diff_out = (diff_out[:cutoff] +
                                f"\n[TRUNCATED: full git diff is {diff_size} bytes; "
                                f"only first {cutoff} bytes shown. "
                                f"Commit message must be based on this summary.\n")
                input_string += "Git diff output:\n" + diff_out + "\n"
        except Exception:
            pass

    if os.getenv("TMUX") != "" and not args.no_screen:
        input_string += "This is the users terminal:\n"
        ib = subprocess.check_output(
                f"tmux capture-pane -p -t {args.target} -S -{args.scrollback}",
                shell=True
                )
        input_string += ib.decode("utf-8")
        # remove mait invocation from prompt (hopefully)
        if args.target == default_tmux_target:
            input_string = "\n".join(input_string.strip().splitlines()[0:-1])
        input_string += "\n"

    if args.verbose:
        print("Flags: ".ljust(VERBOSE_LEN), end="")
        print(",\n".ljust(VERBOSE_LEN+2).join(str(vars(args)).split(",")))
        print("Prompt prefix: ".ljust(VERBOSE_LEN), end="")
        print(" ".join(arg_input))
        print("Using model:".ljust(VERBOSE_LEN), end="")
        print(args.model)
        print("Target:".ljust(VERBOSE_LEN), end="")
        print(args.target)
        print("\n")

    # Add system info to prompt
    with open("/etc/os-release") as f:
        system_info = {f: v for f, v in
                       (x.strip().split("=") for x in f.readlines())
                       }
    input_system_prompt += f"user os: {system_info.get('NAME', 'linux')}"

    # add input from command invocation
    prefix_input = ""
    if len(arg_input) > 0:
        prefix_input = " ".join(arg_input)
    elif args.git:
        prefix_input = "Stage changes, write a concise git commit message based on the status and diff, and push: git add <files>; git commit -m \"...\"; git push"
    if args.file is not None:
        with open(args.file) as f:
            prefix_input += f.read()

    # start processing input
    if prefix_input != "":
        prompt = prefix_input + ":\n\n" + input_string
    else:
        prompt = input_string

    if args.add_stackexchange:
        prompt = auto_overflow(prompt)

    if prefix_input + input_string != "":
        process_prompt(prompt, input_system_prompt, args.model)
    else:
        print("No input. Are you inside tmux?")


def main():
    global args
    try:
        run_muxmait()
    except KeyboardInterrupt:
        print(" KeyboardInterrupt")


model_dict = {
        "nh": "openrouter/nousresearch/hermes-3-llama-3.1-405b:free",
        "gf": "gemini/gemini-3-flash-preview",
        "gt": "gemini/gemini-3.1-flash-lite-preview",
        "gp": "gemini/gemini-3.1-pro-preview",
        "cs": "anthropic/claude-3-7-sonnet-latest",
        "ch": "anthropic/claude-3-5-haiku-latest",
        "o4m": "openai/gpt-4o-mini",
        "o4o": "openai/gpt-4o",
        "xg": "xai/grok-2",
        "g2f": "gemini/gemini-2.5-flash",
        "g2fl": "gemini/gemini-2.5-flash-lite",
        "g2p": "gemini/gemini-2.5-pro",
        "qw": "openrouter/qwen/qwen3.6-plus",
        "gm": "gemini/gemma-4-31b-it",
        "gfl": "gemini/gemini-flash-lite-latest",
        "g35fl": "gemini/gemini-3.5-flash-lite",
        "g38f": "gemini/gemini-3.8-flash",
        "orf": "openrouter/free",
        "q38f": "openrouter/qwen/qwen3.8-27b:free",
        "nlf": "openrouter/nvidia/nemotron-3.5-lightning:free",
        }

# Base URLs for different providers
base_urls = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "xai": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1"
}

# Model configurations with their respective API keys and base URLs
direct_models = {
    "gemini/gemini-flash-lite-latest": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3.5-flash-lite": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3.8-flash": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3-flash-preview": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3.1-pro-preview": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3.1-flash-lite-preview": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-3.5-flash-lite-preview": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-2.5-flash": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-2.5-flash-lite": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemini-2.5-pro": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemma-4-31b-it": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "gemini/gemma-4-26b-it": {
        "api_key": "GEMINI_API_KEY",
        "base_url": base_urls["gemini"]
    },
    "openai/gpt-4o-mini": {
        "api_key": "OPENAI_API_KEY",
        "base_url": base_urls["openai"]
    },
    "openai/gpt-4o": {
        "api_key": "OPENAI_API_KEY",
        "base_url": base_urls["openai"]
    },
    "xai/grok-2": {
        "api_key": "XAI_API_KEY",
        "base_url": base_urls["xai"]
    },
    "openrouter/nousresearch/hermes-3-llama-3.1-405b:free": {
        "api_key": "OPENROUTER_API_KEY",
        "base_url": base_urls["openrouter"]
    },
    "openrouter/qwen/qwen3.6-plus": {
        "api_key": "OPENROUTER_API_KEY",
        "base_url": base_urls["openrouter"]
    },
    "openrouter/free": {
        "api_key": "OPENROUTER_API_KEY",
        "base_url": base_urls["openrouter"]
    },
    "openrouter/qwen/qwen3.8-27b:free": {
        "api_key": "OPENROUTER_API_KEY",
        "base_url": base_urls["openrouter"]
    },
    "openrouter/nvidia/nemotron-3.5-lightning:free": {
        "api_key": "OPENROUTER_API_KEY",
        "base_url": base_urls["openrouter"]
    },
}


default_tmux_target = (
            subprocess
            .check_output("tmux display-message -p '#S:#I.#P'", shell=True)
            .decode("utf-8")
            .strip()
        )

_max_k = max(len(k) for k in model_dict)
model_list_str = "\n".join(f"  {k + ':':<{_max_k + 1}}  {v}" for k, v in model_dict.items())

parser = argparse.ArgumentParser(
    prog="muxmait",
    description="ai terminal assistant",
    epilog=f"models:\n{model_list_str}\n\neschaton",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)

parser.add_argument(
    "-A", "--auto", help="automatically run command. be weary",
    action="store_true"
)
parser.add_argument(
    "-r", "--recursive", help="add ;mait to the end of the ai suggested command",
    action="store_true"
)
parser.add_argument(
    "-m", "--model", help="Set model. Default is gemini/gemini-flash-lite-latest (the chain above applies in git mode). You can also pass a shorthand to select from model list",
    default=None
)
parser.add_argument(
    "-q", "--quiet", help="only return command no explanation",
    action="store_true"
)
parser.add_argument(
    "-v", "--verbose", help="verbose mode",
    action="store_true"
)
parser.add_argument(
    "--debug", help="skips api request and sets message to something mundane",
    action="store_true"
)
parser.add_argument(
    "-t", "--target", help="give target tmux pane to send commands to",
    default=default_tmux_target,
)
parser.add_argument(
    "--log", help="log output to given file"
)
parser.add_argument(
    "--log-commands", help="log only commands to file"
)
parser.add_argument(
    "--file", help="read input from file and append to prefix prompt"
)
parser.add_argument(
    "-S", "--scrollback",
    help="""Scrollback lines to include in prompt.
    Without this only visible pane contents are included""",
    default=0, type=int
)
parser.add_argument(
    "--system-prompt", help="File containing custom system prompt",
)
parser.add_argument(
    "--delay", help="amount of time to delay when using auto", default=2.0, type=float
)
parser.add_argument(
    "-c", "--add-stackexchange", help="if set adds context from stack overflow",
    action="store_true",
)
parser.add_argument(
    "-M", "--model-stackexchange", help="Model to use in order to create google search query for stack exchange content",
    default=DEFAULT_MODEL
)
parser.add_argument(
    "-T", "--thinking-level", help="Set thinking level for Gemini models. Default is minimal",
    choices=['minimal', 'low', 'medium', 'high'],
    default="minimal"
)
parser.add_argument(
    "-g", "--git", help="git commit helper: uses git status and git diff, skips screen capture, and prompts for git add; git commit -m '...'; git push. Falls back through gemini/gemini-3-flash-preview ('gf'), gemini/gemini-3.1-flash-lite-preview ('gt'), nemotron-3.5-lightning:free ('nlf'), the gemini flash-lites, then openrouter/free ('orf'), giving each model 10s before moving on",
    action="store_true"
)
parser.add_argument(
    "--max-diff", help="maximum git diff size in bytes included in the git-mode prompt (default: 100000)",
    default=100000, type=int
)
parser.add_argument(
    "-N", "--no-screen", help="do not capture or read tmux screen scrollback",
    action="store_true"
)
parser.add_argument(
    "--no-paste", help="do not send the suggested command to tmux (print/log only)",
    action="store_true"
)

if __name__ == "__main__":
    main()
