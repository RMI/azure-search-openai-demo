import json
import logging
import re
import time
from pathlib import Path

import requests
from azure.ai.generative.evaluate import evaluate

from . import service_setup

logger = logging.getLogger("scripts")


def send_question_to_target(question: str, target_url: str, parameters: dict = {}, raise_error=False):
    headers = {"Content-Type": "application/json"}
    body = {
        "messages": [{"content": question, "role": "user"}],
        "stream": False,
        "context": parameters,
    }
    try:
        r = requests.post(target_url, headers=headers, json=body)
        response_dict = r.json()

        try:
            answer = response_dict["choices"][0]["message"]["content"]
            data_points = response_dict["choices"][0]["context"]["data_points"]["text"]
            context = "\n\n".join(data_points)
        except Exception:
            raise ValueError(
                "Response does not adhere to the expected schema. "
                "Either adjust the app response or adjust send_question_to_target() in evaluate.py "
                f"to match the actual schema.\nResponse: {response_dict}"
            )

        response_obj = {"question": question, "answer": answer, "context": context}
        return response_obj
    except Exception as e:
        if raise_error:
            raise e
        return {
            "question": question,
            "answer": str(e),
            "context": str(e),
        }


def truncate_for_log(s: str, max_length=30):
    return s if len(s) < max_length else s[:max_length] + "..."


def load_jsonl(path: Path) -> list[dict()]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f.readlines()]


def run_evaluation(
    openai_config: dict,
    testdata_path: Path,
    results_dir: Path,
    target_url: str,
    target_parameters={},
    num_questions=None,
):
    logger.info("Running evaluation using data from %s", testdata_path)
    testdata = load_jsonl(testdata_path)
    if num_questions:
        logger.info("Limiting evaluation to %s questions", num_questions)
        testdata = testdata[:num_questions]

    logger.info("Sending a test question to the target to ensure it is running...")
    try:
        target_data = send_question_to_target(
            "What information is in your knowledge base?", target_url, target_parameters, raise_error=True
        )
        logger.info(
            'Successfully received response from target: "question": "%s", "answer": "%s", "context": "%s"',
            truncate_for_log(target_data["question"]),
            truncate_for_log(target_data["answer"]),
            truncate_for_log(target_data["context"]),
        )
    except Exception as e:
        logger.error("Failed to send a test question to the target due to error: \n%s", e)
        return False

    # Wrap the target function so that it can be called with a single argument
    async def wrap_target(question: str):
        return send_question_to_target(question, target_url, target_parameters)

    logger.info("Starting evaluation...")
    gpt_metrics = ["gpt_coherence", "gpt_relevance", "gpt_groundedness"]
    results = evaluate(
        target=wrap_target,
        data=testdata,
        task_type="qa",
        metrics_list=gpt_metrics,
        model_config=openai_config,
        data_mapping={
            # Must match qa.jsonl
            "questions": "question",  # column of data providing input to model
            "y_test": "truth",  # column of data providing ground truth answer, optional for default metrics
            # Must match return value of target function
            "contexts": "context",  # column of data providing context for each input
            "y_pred": "answer",  # column of data providing output from model
        },
        tracking=False,
        output_path=results_dir,
    )

    logger.info("Evaluation calls have completed. Calculating overall metrics now...")
    eval_results_filename = list(results.artifacts.keys())[0]
    with open(results_dir / eval_results_filename, encoding="utf-8") as f:
        questions_with_ratings = [json.loads(question_json) for question_json in f.readlines()]

    metrics = {
        metric_name: {
            "mean_rating": round(results.metrics_summary[f"mean_{metric_name}"], 2),
            "pass_count": 0,
            "pass_rate": 0,
        }
        for metric_name in gpt_metrics
    }
    total_length = 0
    max_length = 0
    min_length = 9999999999
    total_with_citation = 0

    def passes_threshold(rating):
        return int(rating) >= 4

    for ind, question_with_rating in enumerate(questions_with_ratings):
        total_length += len(question_with_rating["answer"])
        max_length = max(max_length, len(question_with_rating["answer"]))
        min_length = min(min_length, len(question_with_rating["answer"]))
        total_with_citation += 1 if re.search(r"\[[^\]]+\]", question_with_rating["answer"]) else 0
        for metric_name in gpt_metrics:
            if passes_threshold(question_with_rating[metric_name]):
                metrics[metric_name]["pass_count"] += 1
            metrics[metric_name]["pass_rate"] = round(metrics[metric_name]["pass_count"] / (ind + 1), 2)
    metrics["answer_length"] = {
        "total": total_length,
        "mean": round(total_length / len(questions_with_ratings), 2),
        "max": max_length,
        "min": min_length,
    }
    metrics["answer_has_citation"] = {
        "total": total_with_citation,
        "rate": round(total_with_citation / len(questions_with_ratings), 2),
    }

    # summary statistics
    with open(results_dir / "summary.json", "w", encoding="utf-8") as summary_file:
        summary_file.write(json.dumps(metrics, indent=4))

    with open(results_dir / "evaluate_parameters.json", "w", encoding="utf-8") as parameters_file:
        parameters = {
            "evaluation_gpt_model": openai_config["model"],
            "evaluation_timestamp": int(time.time()),
            "testdata_path": str(testdata_path),
            "target_url": target_url,
            "target_parameters": target_parameters,
            "num_questions": num_questions,
        }
        parameters_file.write(json.dumps(parameters, indent=4))
    logger.info("Evaluation results saved in %s", results_dir)
    return True


def process_config(obj: dict):
    """Replace special markers in a config dict with their values:
    * <TIMESTAMP> with current timestamp
    * <READFILE> with contents of file
    """
    if isinstance(obj, dict):
        for key in obj:
            if isinstance(obj[key], dict):
                process_config(obj[key])
            elif isinstance(obj[key], str) and "<TIMESTAMP>" in obj[key]:
                logger.info("Replaced %s in config with timestamp", key)
                obj[key] = obj[key].replace("<TIMESTAMP>", str(int(time.time())))
            elif isinstance(obj[key], str) and "<READFILE>" in obj[key]:
                with open(obj[key].replace("<READFILE>", ""), encoding="utf-8") as f:
                    logger.info("Replaced %s in config with contents of %s", key, f.name)
                    obj[key] = f.read()


def run_evaluate_from_config(working_dir, config_path, num_questions):
    config_path = working_dir / Path(config_path)
    logger.info("Running evaluation from config %s", config_path)
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
        process_config(config)

    results_dir = working_dir / Path(config["results_dir"])

    evaluation_run_complete = run_evaluation(
        openai_config=service_setup.get_openai_config(),
        testdata_path=working_dir / config["testdata_path"],
        results_dir=results_dir,
        target_url=config["target_url"],
        target_parameters=config.get("target_parameters", {}),
        num_questions=num_questions,
    )

    if evaluation_run_complete:
        results_config_path = results_dir / "config.json"
        logger.info("Saving original config file back to to %s", results_config_path)
        with open(config_path, encoding="utf-8") as input_config:
            with open(results_config_path, "w", encoding="utf-8") as output_config:
                output_config.write(input_config.read())
    else:
        logger.error("Evaluation was terminated early due to an error ⬆")