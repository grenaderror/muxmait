#!/usr/bin/env python3
try:
    from typing import cast
    import sys
    import os
    import subprocess
    import re
    import argparse
    from time import sleep
except KeyboardInterrupt:
    print(" KeyboardInterrupt")
    quit()


VERBOSE_LEN = 20
YOUR_SITE_URL = ""
YOUR_APP_NAME = "muxmait"

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


def get_response_litellm(prompt: str, system_prompt: str, model: str) -> str:
    import litellm
    from litellm.types.utils import ModelResponse
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

    response = cast(ModelResponse, litellm.completion(
        model=model,
        messages=messages,
        temperature=1,
        stop=["```\n"],
        thinking=thinking_params
    ))

    try:
        return response['choices'][0]['message']['content']
    except (AttributeError, KeyError):
        print("unexpected output")
        print(response)
        quit()


def get_response_direct(prompt: str, system_prompt: str, model: str) -> str:
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

    data = {
        "model": model[model.find("/")+1:],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 1.0,
        "stop": ["```\n"]
    }

    if "gemini" in model.lower() or "gemini" in base_url.lower():
        data["reasoning_effort"] = args.thinking_level


    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print("unexpected output")
        try:
            if 'response' in locals():
                print(response.json())
            else:
                print(e)
        except:
            print(e)
        quit()


def get_response(prompt: str, system_prompt: str, model: str) -> str:
    if args.verbose:
        print("getting response")
        print(f"using model {model}")
        print("use litellm " + str(model not in direct_models))
    response: str
    if args.debug:
        response = get_response_debug(prompt, system_prompt, model)
    elif model in direct_models:
        response = get_response_direct(prompt, system_prompt, model)
    else:
        response = get_response_litellm(prompt, system_prompt, model)
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

    response = get_response(prompt, system_prompt, model)
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
    command = clean_command(command)

    if args.log_commands is not None:
        with open(args.log_commands, 'a') as f:
            f.write(command+"\n")

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

    # send command to shell prompt
    subprocess.run(
            f'tmux send-keys -t {args.target} "{command}"', shell=True
            )
    """ tmux send-keys on own pane will put output in front of ps and
    on prompt this keeps that output from moving the ps. If we are sending
    remote we do not need to worry about this. """
    if args.target == default_tmux_target:
        print("\n")

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
    markup_output.append(f"### Question \n{a.get_text(strip=True)}\n")

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

            # Extract link. Note: Google links are often redirects,
            # this gets the actual link shown
            link = result.find('a')['href']

            results.append(link)
        except:
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

    # get input from stdin or tmux scrollback
    input_string: str = ""
    if not sys.stdin.isatty():
        input_string += "User input piped in from command:\n"
        input_string += "".join(sys.stdin)
        input_string += "\n"
    if os.getenv("TMUX") != "":
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
        "g2p": "gemini/gemini-2.5-pro",
        "qw": "openrouter/qwen/qwen3.6-plus",
        "gm": "gemini/gemma-4-31b-it",
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
    "gemini/gemini-2.5-flash": {
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
}


default_tmux_target = (
            subprocess
            .check_output("tmux display-message -p '#S:#I.#P'", shell=True)
            .decode("utf-8")
            .strip()
        )

parser = argparse.ArgumentParser(
    prog="muxmait",
    description="ai terminal assistant",
    epilog="eschaton",
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
    "-m", "--model", help=f"Set model. Default is {model_dict["gf"]}. You can also pass a number to select from model list",
    default=model_dict["gf"]
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
    default="gemini/gemini-3-flash-preview"
)
parser.add_argument(
    "-T", "--thinking-level", help="Set thinking level for Gemini models. Default is minimal",
    choices=['minimal', 'low', 'medium', 'high'],
    default="minimal"
)

if __name__ == "__main__":
    main()
