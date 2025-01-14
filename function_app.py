import azure.functions as func
import azure.durable_functions as df
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from openai import OpenAI
import logging
import re
import os
import tempfile

myApp = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

# クライアント関数
@myApp.function_name("durable_openai")
@myApp.route(route="")
@myApp.durable_client_input(client_name="client")
async def durable_openai(req: func.HttpRequest, client: df.DurableOrchestrationClient):
    logging.info("[client-info] Recieved an HTTP request. The client function was triggered successfully.")
    print("[client-print] Recieved an HTTP request. The client function was triggered successfully.")

    req_body = req.get_json()
    file_name = req_body.get("fileName")
    target_lang = req_body.get("targetLang")

    input = {"file_name": file_name, "target_lang": target_lang}

    function_name = "orch_openai"

    instance_id = await client.start_new(orchestration_function_name=function_name, client_input=input)
    response = client.create_check_status_response(req, instance_id)
    return response

# オーケストレーター関数
@myApp.orchestration_trigger(context_name="context")
def orch_openai(context: df.DurableOrchestrationContext):
    tasks = []

    file_name = context.get_input()["file_name"]
    target_lang = context.get_input()["target_lang"]

    logging.info(f"[orch-info] file_name: {file_name}")
    logging.info(f"[orch-info] target_lang: {target_lang}")
    print(f"[orch-print] file_name: {file_name}")
    print(f"[orch-print] target_lang: {target_lang}")

    activity_name = "actv_openai"
    input = {"file_name": file_name, "target_lang": target_lang}

    task = context.call_activity(name=activity_name, input_=input)
    tasks.append(task)
    results = yield context.task_all(tasks)
    return results

# アクティビティ関数
# なぜか input_name="file_name" だけはエラーになる
@myApp.activity_trigger(input_name="input")
def actv_openai(input: dict):
    openai_api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=openai_api_key)

    file_name = input.get("file_name")
    target_lang = input.get("target_lang")
    file_name_only = re.split("[.]", file_name)[-2]
    vocals_file_name = f"vocals_{file_name_only}.wav"

    logging.info(f"[actv-info] file_name_only: {file_name_only}")
    logging.info(f"[actv-info] vocals_file_name: {vocals_file_name}")

    print(f"[actv-print] file_name_only: {file_name_only}")
    print(f"[actv-print] vocals_file_name: {vocals_file_name}")

    account_url = os.getenv("account_url")
    container_separated = os.getenv("container_separated")
    container_lyrics = os.getenv("container_lyrics")

    # ストレージへの認証
    default_credential = DefaultAzureCredential()

    # BlobServiceClient の取得
    blob_service_client = BlobServiceClient(account_url, credential=default_credential)

    # ContainerClient の取得
    container_client_separated = blob_service_client.get_container_client(container=container_separated)
    container_client_lyrics = blob_service_client.get_container_client(container=container_lyrics)

    # input blob の取得
    inputblob = container_client_separated.get_blob_client(blob=vocals_file_name)

    # InputStream を bytes として扱うために BytesIO に読み込む
    # input_bytes = io.BytesIO(inputblob.read())
    input_bytes = inputblob.download_blob()

    temp_vocals = None
    temp_txt = None

    with (
        tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_vocals
    ):
        # inputblob の内容を書き込む
        # temp_vocals.write(input_bytes.getvalue())
        temp_vocals.write(input_bytes.readall())

        logging.info(f"[actv-info] temp_vocals file path: {temp_vocals.name}")
        print(f"[actv-print] temp_vocals file path: {temp_vocals.name}")

        # 初期化
        transcription = None
        with open(temp_vocals.name, mode="rb") as audio:
            try:
                # Speech-to-Text
                logging.info("[actv-info] Now Speech-to-Text proccessing with whisper...")
                print("[actv-print] Now Speech-to-Text proccessing with whisper...")

                if target_lang == "auto-detect":
                    transcription = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"]
                    )
                else:
                    transcription = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                        language=target_lang
                    )

            except Exception as e:
                logging.error(f"[actv-error] Some error occured on whisper!! {str(e)}")
                print(f"[actv-print] Some error occured on whisper!! {str(e)}")
            else:
                lyrics_file_name = f"vocals_{file_name_only}.txt"
                logging.info(f"[actv-info] lyrics_file_name: {lyrics_file_name}")
                print(f"[actv-print] lyrics_file_name: {lyrics_file_name}")
            finally:
                logging.info("[actv-info] At least an attempt to process Speech-to-Text is done.")
                print("[actv-print] At least an attempt to process Speech-to-Text is done.")

        if transcription is not None:
            # テキストをセグメントで改行
            with (
                tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8") as temp_txt
            ):
                with open(temp_txt.name, mode="w", encoding="utf-8") as file:
                    for seg in transcription.segments:
                        file.write(f"{seg.text}\n")

                # コンテナーに txt ファイルをアップロード
                with open(temp_txt.name, mode="rb") as target:
                    container_client_lyrics.upload_blob(name=lyrics_file_name, data=target, overwrite=True)

            logging.info("[actv-info] A lyrics file was successfully uploaded.")
            print("[actv-print] A lyrics file was successfully uploaded.")
        else:
            logging.error("[actv-error] No transcription available. Skipping file upload.")
            print("[actv-print] No transcription available. Skipping file upload.")


    # オリジナル音声ファイルの削除
    container_client_delete = blob_service_client.get_container_client(container=container_separated)

    logging.info("[actv-info] Now deleting vocal track...")
    print("[actv-print] Now deleting the vocal track...")
    container_client_delete.delete_blob(blob=vocals_file_name)

    # 一時ファイルの削除
    logging.info("[actv-info] Now deleting the temp file...")
    print("[actv-print] Now deleting the temp file...")

    if temp_vocals and os.path.exists(temp_vocals.name):
        os.remove(temp_vocals.name)

    if temp_txt and os.path.exists(temp_txt.name):
        os.remove(temp_txt.name)

    # レスポンス
    return "Speech-to-text processing by whisper was executed successfully."
