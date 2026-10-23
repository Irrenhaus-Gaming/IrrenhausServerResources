"""mcdeploy: validate, boot-test, render and deploy the Irrenhaus Minecraft configs.

Every check fails hard: any problem raises McDeployError, which the CLI turns into exit code 1.
There are no warn-and-continue paths.
"""
