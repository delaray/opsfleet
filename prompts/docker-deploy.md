# Overview
1. I would like to be able to run the interactive chat from a single Docker container. 
2. The container should able to communicate live with Gemini and BigQuery in order to answer data analysis questions
3. There should be a volume for storing reports, logs and traces

# Task
1. Create the necessay Dockerfile and Docker Compose files in or to fully containerize the application.
Keep in mind that the Gemini key in the .env file and other secrets are needed and should be provided in a secure manner to the image.
2. Ensure the image build is successful
3. Build the image locally and tag it delaray/datachat:1.0.0
4. Login to my DockerHub account
5. Push the built image to delaray/datachat:1.0.0
6. Test pulling the image from DockerHub
7. Document the Docker & Docker Compose files in docs/docker-image.md
