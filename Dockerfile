# Use official AWS Lambda Python 3.12 base image
FROM public.ecr.aws/lambda/python:3.12

# Install only essential dependencies for PyMuPDF
RUN microdnf update -y && \
    microdnf install -y gcc-c++ mesa-libGL-devel

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire src directory and service account if exists
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY service-account.json* ${LAMBDA_TASK_ROOT}/

# Default entry point: Phase 1 Ingestion Controller
CMD [ "src.lambda.ingestion_controller.lambda_handler" ]
