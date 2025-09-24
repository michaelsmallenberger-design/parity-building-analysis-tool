# Use an official Python base image
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /app

# Install the missing system library required by OpenCV
RUN apt-get update && apt-get install -y libgl1-mesa-glx

# Copy the requirements file first to leverage Docker's layer caching
COPY requirements.txt requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# The CMD to run will be taken from your Procfile by Railway