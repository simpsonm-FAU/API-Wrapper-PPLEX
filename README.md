# PersonalPlex API Gateway

An authenticated API gateway for the PersonalPlex/Moshi speech-to-speech model. This gateway provides API key management, rate limiting, and a RESTful endpoint for offline inference, alongside the real-time WebSocket proxy.

## Features

- **API Key Authentication**: Secure your Moshi instance with hashed API keys.
- **Rate Limiting**: Prevent abuse with configurable rate limits per key.
- **Offline Inference**: REST endpoint to process WAV files without real-time streaming constraints.
- **WebSocket Proxy**: Full-duplex streaming support for real-time conversation.

## Requirements

- Python 3.8+
- [Moshi](https://github.com/kyutai-labs/moshi) (or PersonalPlex fork)
- Audio drivers (if running client locally)

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/your-username/personalplex-gateway.git
   cd personalplex-gateway
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Offline Environment Setup

To run this gateway in an offline environment where you cannot access external PyPI repositories or Hugging Face, follow these steps:

### 1. Pre-download Dependencies

On an online machine, download the wheels for the requirements:

```bash
mkdir wheels
pip download -r requirements.txt -d wheels
```

Transfer the `wheels` directory to your offline machine and install:

```bash
pip install --no-index --find-links=wheels -r requirements.txt
```

### 2. Set Up Moshi/PersonaPlex Locally

Ensure you have the Moshi or PersonalPlex repository cloned and installed on the offline machine.

1.  **Clone the Repo**:
    ```bash
    git clone https://github.com/kyutai-labs/moshi.git /opt/personaplex
    cd /opt/personaplex
    pip install .
    ```

2.  **Download Model Weights**:
    You will need the model weights (e.g., `moshiko-pytorch-bf16`). Download them from Hugging Face on an online machine and transfer them to a local directory on the offline machine (e.g., `/opt/models/moshi`).

### 3. Configuration

You can configure the gateway using environment variables. Create a `.env` file or export them in your shell.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `MOSHI_HOST` | Hostname of the Moshi server | `localhost` |
| `MOSHI_PORT` | Port of the Moshi server | `8998` |
| `PERSONAPLEX_REPO` | **Critical for Offline**: Path to the local Moshi repo | `/opt/personaplex` |
| `API_KEYS_FILE` | Path to store API keys | `api_keys.json` |
| `ADMIN_SECRET` | Secret for admin endpoints | `change-me-in-production` |

**Example Command (Offline Mode):**

```bash
# Set the path where you cloned Moshi/PersonaPlex
export PERSONAPLEX_REPO="C:/Authentication/personaplex"

# Start the Gateway
uvicorn api_gateway:app --host 0.0.0.0 --port 8000
```

## Usage

### 1. Generating an API Key

On first run, the server will log a default admin key. Use this key to generate persistent keys via the API.

```bash
curl -X POST http://localhost:8000/admin/keys/generate \
  -H "X-Admin-Secret: change-me-in-production" \
  -F "name=my-app"
```

### 2. Real-time Streaming (WebSocket)

Connect to: `ws://localhost:8000/ws/stream?api_key=YOUR_KEY`

### 3. Offline Inference (REST)

Send a WAV file to be processed:

```bash
curl -X POST http://localhost:8000/v1/inference \
  -H "X-API-Key: YOUR_KEY" \
  -F "audio=@input.wav" \
  -F "voice=NATF2" \
  -F "persona=You are a helpful assistant." \
  --output response.wav
```

## Running the Moshi Backend

Ensure the Moshi server is running separately if you are using the WebSocket features:
```bash
python -m moshi.server --port 8998
```

## Running as a Service

To ensure the gateway runs automatically on boot and restarts on failure, set it up as a system service.

### Linux (systemd)

1.  Create a service file at `/etc/systemd/system/personaplex-gateway.service`:

    ```ini
    [Unit]
    Description=PersonaPlex API Gateway
    After=network.target

    [Service]
    User=your-user
    Group=your-group
    WorkingDirectory=/opt/personalplex-gateway
    Environment="PATH=/opt/personalplex-gateway/venv/bin:/usr/local/bin:/usr/bin"
    Environment="MOSHI_HOST=localhost"
    Environment="PERSONAPLEX_REPO=/opt/personaplex"
    ExecStart=/opt/personalplex-gateway/venv/bin/uvicorn api_gateway:app --host 0.0.0.0 --port 8000
    Restart=always

    [Install]
    WantedBy=multi-user.target
    ```

2.  Enable and start the service:

    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable personaplex-gateway
    sudo systemctl start personaplex-gateway
    ```

### Windows (NSSM)

For Windows servers, use [NSSM (Non-Sucking Service Manager)](https://nssm.cc/):

1.  Download NSSM and extract it.
2.  Open a terminal as Administrator.
3.  Run: `nssm install PersonaplexGateway`
4.  In the GUI:
    -   **Path**: Path to your `python.exe` (or venv python).
    -   **Startup directory**: `C:\path\to\personalplex-gateway`
    -   **Arguments**: `-m uvicorn api_gateway:app --host 0.0.0.0 --port 8000`
5.  Click **Install service**.
6.  Start it: `nssm start PersonaplexGateway`
